from __future__ import annotations
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from concurrent.futures import ThreadPoolExecutor, as_completed

from sketchfab_client import (
    SketchfabClient,
    PUSH_BATCH_SIZE,
    PUSH_BATCH_GAP_SEC,
    PRELOAD_WORKERS,
    PUSH_VERIFY_MEMBERSHIP,
    PUSH_VERIFY_AFTER,
)
from state import uid_col
from collection_match import match_collection_name
from sync_status import format_duration


logger = logging.getLogger(__name__)

ASSIGNED_COL = "Assigned Collection(s)"
ALREADY_COL = "Already In Collection(s)"
# Local shelf for models Sketchfab refuses to collect (e.g. someone else's private).
# Not a real Sketchfab collection — kept in Already In so they leave Unassigned/Pending
# while still carrying intended collection names for N/W filters.
UNLISTED_COLLECTION = "Unlisted"
ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]


def _is_private_collect_blocked(msg: str) -> bool:
    low = (msg or "").lower()
    return "private models of others" in low or "cannot collect private" in low


def is_unlisted_cell(cell) -> bool:
    """True if Already In (or similar) lists the local Unlisted shelf."""
    raw = _cell_text(cell)
    if not raw:
        return False
    return any(p.strip().casefold() == UNLISTED_COLLECTION.casefold() for p in raw.split(","))


def _cell_text(cell) -> str:
    """Normalize workbook/pandas cells — treat NaN/None/'nan' as empty."""
    if cell is None:
        return ""
    if isinstance(cell, float) and pd.isna(cell):
        return ""
    try:
        if pd.isna(cell):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(cell).strip()
    if not s or s.casefold() in {"nan", "none", "null", "<na>"}:
        return ""
    return s


def _csv_parts(cell) -> list[str]:
    raw = _cell_text(cell)
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip() and p.strip().casefold() not in {"nan", "none", "null"}]


def _already_names(cell) -> set[str]:
    return {p.casefold() for p in _csv_parts(cell)}


def pending_push_rows(liked_df: pd.DataFrame) -> list[dict]:
    """
    Local queue preview: Assigned collections not yet listed in Already In.
    (Push also skips Sketchfab membership; this is the workbook-side pending list.)
    """
    if liked_df is None or liked_df.empty or ASSIGNED_COL not in liked_df.columns:
        return []
    uc = uid_col(liked_df)
    if not uc:
        return []
    nc = "Name" if "Name" in liked_df.columns else ("Model Name" if "Model Name" in liked_df.columns else "")
    rows: list[dict] = []
    for _, row in liked_df.iterrows():
        uid = _cell_text(row.get(uc))
        if not uid:
            continue
        want = _csv_parts(row.get(ASSIGNED_COL))
        if not want:
            continue
        already = _already_names(row.get(ALREADY_COL)) if ALREADY_COL in liked_df.columns else set()
        for coll in want:
            if coll.casefold() in already:
                continue
            rows.append(
                {
                    "uid": uid,
                    "name": _cell_text(row.get(nc)) if nc else uid,
                    "collection": coll,
                    "author": _cell_text(row.get("Author")),
                    "thumb": _cell_text(row.get("Thumbnail") or row.get("Thumbnail HD")),
                    "push_sent": _cell_text(row.get("Push Sent")).lower() in {"yes", "y", "true", "1"},
                    "already": _cell_text(row.get(ALREADY_COL)),
                    "assigned_at": _cell_text(row.get("Assigned At")),
                }
            )
    return rows


def pending_push_grouped(liked_df: pd.DataFrame) -> list[dict]:
    """Same queue as pending_push_rows, but one entry per model with all pending collections."""
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for r in pending_push_rows(liked_df):
        uid = r["uid"]
        g = grouped.get(uid)
        if g is None:
            g = {
                "uid": uid,
                "name": r["name"],
                "author": r.get("author", ""),
                "thumb": r.get("thumb", ""),
                "assigned_at": r.get("assigned_at", ""),
                "collections": [],
            }
            grouped[uid] = g
            order.append(uid)
        if r["collection"] not in g["collections"]:
            g["collections"].append(r["collection"])
        # Keep the newest stamp if rows somehow differ.
        at = str(r.get("assigned_at") or "").strip()
        if at and (not g.get("assigned_at") or at > str(g.get("assigned_at") or "")):
            g["assigned_at"] = at
    return [grouped[u] for u in order]


def _build_collection_maps(collections_df: pd.DataFrame) -> tuple[dict[str, str], dict[str, str]]:
    name_to_uid: dict[str, str] = {}
    lower_to_name: dict[str, str] = {}
    for _, row in collections_df.iterrows():
        name = str(row.get("Collection Name") or "").strip()
        uid = str(row.get("Collection UID") or "").strip()
        if not name or not uid:
            continue
        name_to_uid[name] = uid
        lower_to_name.setdefault(name.lower(), name)
    return name_to_uid, lower_to_name


def _resolve_collection_name(name: str, name_to_uid: dict[str, str], lower_to_name: dict[str, str]) -> str | None:
    name = name.strip()
    if not name:
        return None
    if name in name_to_uid:
        return name
    hit = lower_to_name.get(name.lower())
    if hit:
        return hit
    # Enemies → Enemy, Foods → Food, etc.
    return match_collection_name(name, list(name_to_uid.keys()))


def _pending_assign_names(row) -> list[str]:
    """Assigned collections on this row that the workbook doesn't already mark as synced.

    Trusting Already In keeps Push aligned with the Pending Push queue (and avoids
    re-planning every past assignment whenever a live collection read comes back stale).
    """
    want = _csv_parts(row.get(ASSIGNED_COL))
    if not want:
        return []
    already = _already_names(row.get(ALREADY_COL))
    return [c for c in want if c.casefold() not in already]


def _assigned_collection_names(liked_df: pd.DataFrame, name_to_uid: dict[str, str], lower_to_name: dict[str, str]) -> set[str]:
    needed: set[str] = set()
    for _, row in liked_df.iterrows():
        for coll_name in _pending_assign_names(row):
            resolved = _resolve_collection_name(coll_name, name_to_uid, lower_to_name)
            if resolved:
                needed.add(resolved)
    return needed


def _plan_push_ops(
    liked_df: pd.DataFrame,
    name_to_uid: dict[str, str],
    lower_to_name: dict[str, str],
    existing: dict[str, set[str]],
    *,
    should_cancel: CancelFn | None = None,
) -> tuple[list[tuple[str, str, str]], int, set[str]]:
    """Return (ops, already_present, unknown_collection_names)."""
    ucol = uid_col(liked_df)
    ops: list[tuple[str, str, str]] = []
    already_present = 0
    unknown_collections: set[str] = set()

    for _, row in liked_df.iterrows():
        if should_cancel and should_cancel():
            break
        model_uid = str(row.get(ucol) if ucol else row.get("Model UID") or row.get("UID") or "").strip()
        if not model_uid:
            continue
        for coll_name in _pending_assign_names(row):
            resolved = _resolve_collection_name(coll_name, name_to_uid, lower_to_name)
            if not resolved:
                unknown_collections.add(coll_name)
                continue
            coll_uid = name_to_uid.get(resolved, "").strip()
            if not coll_uid:
                unknown_collections.add(coll_name)
                continue
            if model_uid in existing.get(resolved, set()):
                already_present += 1
                continue
            ops.append((coll_uid, model_uid, resolved))
    return ops, already_present, unknown_collections


def _merge_live_collection_maps(
    client: SketchfabClient,
    name_to_uid: dict[str, str],
    lower_to_name: dict[str, str],
) -> int:
    added = 0
    try:
        for coll in client.get_collections():
            name = str(coll.name or "").strip()
            uid = str(coll.uid or "").strip()
            if not name or not uid:
                continue
            if name not in name_to_uid:
                added += 1
            name_to_uid[name] = uid
            lower_to_name.setdefault(name.lower(), name)
    except Exception as exc:
        logger.warning("Live collection refresh failed: %s", exc)
    return added


def _first_pending_model_for_collection(liked_df: pd.DataFrame, coll_name: str) -> str | None:
    want = coll_name.strip()
    if not want:
        return None
    ucol = uid_col(liked_df)
    for _, row in liked_df.iterrows():
        model_uid = str(row.get(ucol) if ucol else row.get("Model UID") or row.get("UID") or "").strip()
        if not model_uid:
            continue
        for assigned in _pending_assign_names(row):
            if assigned.casefold() == want.casefold():
                return model_uid
    return None


def _ensure_missing_collections(
    client: SketchfabClient,
    liked_df: pd.DataFrame,
    unknown: set[str],
    name_to_uid: dict[str, str],
    lower_to_name: dict[str, str],
    existing: dict[str, set[str]],
    *,
    dry_run: bool,
    on_progress: ProgressFn | None = None,
) -> tuple[list[dict[str, str]], set[str]]:
    """
    Refresh live collection names, then create any still-missing collections on Sketchfab.
    Returns (created [{name, uid}], still_unknown).
    """
    if not unknown:
        return [], set()
    if on_progress:
        on_progress(0, 0, f"Refreshing collections for {len(unknown)} unknown name(s)…")
    _merge_live_collection_maps(client, name_to_uid, lower_to_name)
    still: set[str] = set()
    created: list[dict[str, str]] = []
    for coll_name in sorted(unknown):
        if _resolve_collection_name(coll_name, name_to_uid, lower_to_name):
            continue
        model_uid = _first_pending_model_for_collection(liked_df, coll_name)
        if not model_uid:
            still.add(coll_name)
            continue
        if dry_run:
            name_to_uid[coll_name] = "(dry-run)"
            lower_to_name.setdefault(coll_name.lower(), coll_name)
            existing.setdefault(coll_name, set()).add(model_uid)
            created.append({"name": coll_name, "uid": "", "seed_model": model_uid})
            continue
        if on_progress:
            on_progress(0, 0, f"Creating collection '{coll_name}' on Sketchfab…")
        try:
            coll = client.create_collection(coll_name, [model_uid])
            final_name = str(coll.name or coll_name).strip() or coll_name
            uid = str(coll.uid or "").strip()
            if not uid:
                still.add(coll_name)
                continue
            name_to_uid[final_name] = uid
            lower_to_name.setdefault(final_name.lower(), final_name)
            existing.setdefault(final_name, set()).add(model_uid)
            created.append({"name": final_name, "uid": uid, "seed_model": model_uid})
            logger.info("Created collection '%s' (%s) with seed model %s", final_name, uid, model_uid)
        except Exception as exc:
            logger.warning("Create collection '%s' failed: %s", coll_name, exc)
            still.add(coll_name)
    return created, still


def merge_created_collections(collections_df: pd.DataFrame, created: list[dict]) -> pd.DataFrame:
    """Append newly created Sketchfab collections to the workbook collections frame."""
    if not created:
        return collections_df
    rows = []
    have = set()
    if collections_df is not None and not collections_df.empty:
        for _, row in collections_df.iterrows():
            n = str(row.get("Collection Name") or "").strip()
            u = str(row.get("Collection UID") or "").strip()
            if n:
                have.add(n.casefold())
            if u:
                have.add(u)
    for item in created:
        name = str(item.get("name") or "").strip()
        uid = str(item.get("uid") or "").strip()
        if not name or not uid or name.casefold() in have or uid in have:
            continue
        rows.append(
            {
                "Collection Name": name,
                "Collection UID": uid,
                "Model Count": 1,
                "Model Names": "",
                "Thumbnail": "",
            }
        )
        have.add(name.casefold())
        have.add(uid)
    if not rows:
        return collections_df
    extra = pd.DataFrame(rows)
    if collections_df is None or collections_df.empty:
        return extra
    return pd.concat([collections_df, extra], ignore_index=True)


def _preload_existing(
    client: SketchfabClient,
    name_to_uid: dict[str, str],
    needed_names: set[str],
    on_progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
) -> tuple[dict[str, set[str]], list[str], bool]:
    """Returns (existing, stale_collections, cancelled)."""
    existing: dict[str, set[str]] = {}
    stale_collections: list[str] = []
    items = [(name, name_to_uid.get(name, "")) for name in sorted(needed_names) if name_to_uid.get(name)]
    total = len(items)
    if total == 0:
        return existing, stale_collections, False

    done = 0
    cancelled = False

    def _one(name: str, uid: str) -> tuple[str, set[str], str | None]:
        try:
            return name, set(client.list_models_in_collection(uid)), None
        except Exception as exc:
            logger.warning("Could not list models for collection %s: %s", name, exc)
            return name, set(), name

    workers = max(1, min(PRELOAD_WORKERS, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Wave submissions so Stop can abort before the next wave starts.
        for wave_start in range(0, total, workers):
            if should_cancel and should_cancel():
                cancelled = True
                break
            wave = items[wave_start : wave_start + workers]
            futs = [pool.submit(_one, n, u) for n, u in wave]
            for fut in as_completed(futs):
                if should_cancel and should_cancel():
                    cancelled = True
                name, uids, stale = fut.result()
                existing[name] = uids
                if stale:
                    stale_collections.append(stale)
                done += 1
                if on_progress and (done == 1 or done == total or done % 4 == 0):
                    on_progress(done, total, f"Checking collections {done:,}/{total:,}")
            if cancelled:
                break
    return existing, stale_collections, cancelled


def _post_batch(
    client: SketchfabClient,
    coll_uid: str,
    coll_name: str,
    model_uids: list[str],
) -> tuple[list[str], list[str]]:
    if not model_uids:
        return [], []
    try:
        client.add_models_to_collection(coll_uid, model_uids)
        return list(model_uids), []
    except Exception as exc:
        msg = str(exc)
        if len(model_uids) == 1:
            hint = ""
            if _is_private_collect_blocked(msg):
                hint = " (someone else's private model — Sketchfab blocks collecting those)"
            return [], [f"{model_uids[0]} → {coll_name}: {exc}{hint}"]
        # Rate limits: fail the whole batch — binary-split turns one 429 into N slow POSTs.
        low = msg.lower()
        if "429" in msg or "rate limit" in low or "throttl" in low:
            return [], [f"{u} → {coll_name}: {exc}" for u in model_uids]
        mid = len(model_uids) // 2
        ok: list[str] = []
        errs: list[str] = []
        for chunk in (model_uids[:mid], model_uids[mid:]):
            chunk_ok, chunk_errs = _post_batch(client, coll_uid, coll_name, chunk)
            ok.extend(chunk_ok)
            errs.extend(chunk_errs)
        return ok, errs


def _private_pairs_from_errors(errors: list[str]) -> list[tuple[str, str]]:
    """Parse (model_uid, collection) from private-model failure messages."""
    out: list[tuple[str, str]] = []
    for err in errors:
        if not _is_private_collect_blocked(err):
            continue
        left, _, rest = err.partition(" → ")
        uid = left.strip()
        coll = rest.split(":", 1)[0].strip() if rest else ""
        if uid and coll:
            out.append((uid, coll))
    return out


def _verify_in_collection(
    client: SketchfabClient,
    coll_uid: str,
    claimed: list[str],
    *,
    known_members: set[str] | None = None,
) -> tuple[list[str], list[str], set[str]]:
    """Split claimed UIDs into (verified, missing) after a live membership read."""
    if not claimed:
        return [], [], set(known_members or ())
    try:
        members = set(client.list_models_in_collection(coll_uid))
    except Exception as exc:
        logger.warning("Verify membership failed for %s: %s — trusting POST", coll_uid, exc)
        return list(claimed), [], set(known_members or ())
    verified = [u for u in claimed if u in members]
    missing = [u for u in claimed if u not in members]
    return verified, missing, members


def push(
    liked_df: pd.DataFrame,
    collections_df: pd.DataFrame,
    client: SketchfabClient | None = None,
    dry_run: bool = False,
    on_progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
    verify_membership: bool | None = None,
) -> dict:
    t0 = time.time()
    client = client or SketchfabClient.for_push()
    throttle_waits_before = client.throttle_waits
    name_to_uid, lower_to_name = _build_collection_maps(collections_df)

    def _cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    needed_names = _assigned_collection_names(liked_df, name_to_uid, lower_to_name)
    # Real Push + Dry-run trust workbook Already In (same as Pending queue).
    # Check sync passes verify_membership=True for a live membership crawl.
    if verify_membership is None:
        verify = bool(PUSH_VERIFY_MEMBERSHIP)
    else:
        verify = bool(verify_membership)
    t_preload = time.time()
    cancelled_early = False
    if verify and needed_names:
        existing, stale_collections, cancelled_early = _preload_existing(
            client, name_to_uid, needed_names, on_progress, should_cancel=should_cancel
        )
    else:
        existing, stale_collections = {}, []
        if needed_names and on_progress:
            on_progress(
                0,
                0,
                f"Skipping pre-push membership crawl ({len(needed_names)} colls)",
            )
    preload_sec = round(time.time() - t_preload, 1)

    if cancelled_early or _cancelled():
        return {
            "planned": 0,
            "posted": 0,
            "already_present": 0,
            "failed": 0,
            "dry_run": dry_run,
            "cancelled": True,
            "unknown_collections": [],
            "stale_collections": stale_collections,
            "posted_model_uids": [],
            "posted_pairs": [],
            "unlisted_pairs": [],
            "errors": [],
            "throttle_waits": client.throttle_waits - throttle_waits_before,
            "collections_checked": len(existing) if verify else 0,
            "membership_verified": verify,
            "preload_sec": preload_sec,
            "accepted_not_added": 0,
            "elapsed_sec": round(time.time() - t0, 1),
        }

    created_collections: list[dict[str, str]] = []
    ops, already_present, unknown_collections = _plan_push_ops(
        liked_df, name_to_uid, lower_to_name, existing, should_cancel=should_cancel
    )

    if unknown_collections:
        created_collections, unknown_collections = _ensure_missing_collections(
            client,
            liked_df,
            unknown_collections,
            name_to_uid,
            lower_to_name,
            existing,
            dry_run=dry_run,
            on_progress=on_progress,
        )
        ops, already_present, unknown_collections = _plan_push_ops(
            liked_df, name_to_uid, lower_to_name, existing, should_cancel=should_cancel
        )

    if not ops:
        seed_pairs = [
            (str(c["seed_model"]), str(c["name"]))
            for c in created_collections
            if c.get("seed_model") and c.get("name") and not dry_run
        ]
        lines_unknown = sorted(unknown_collections)
        return {
            "planned": len(seed_pairs),
            "posted": len(seed_pairs),
            "already_present": already_present,
            "failed": 0,
            "dry_run": dry_run,
            "cancelled": _cancelled(),
            "unknown_collections": lines_unknown,
            "stale_collections": stale_collections,
            "posted_model_uids": sorted({u for u, _ in seed_pairs}),
            "posted_pairs": seed_pairs,
            "unlisted_pairs": [],
            "errors": [],
            "throttle_waits": client.throttle_waits - throttle_waits_before,
            "collections_checked": len(needed_names) if verify else 0,
            "membership_verified": verify,
            "preload_sec": preload_sec,
            "accepted_not_added": 0,
            "created_collections": created_collections,
            "elapsed_sec": round(time.time() - t0, 1),
        }

    logger.info(
        "Pushing %d assignments (dry_run=%s, verify_membership=%s, batch=%d)",
        len(ops),
        dry_run,
        verify,
        PUSH_BATCH_SIZE,
    )
    posted = 0
    failed = 0
    processed = 0
    accepted_not_added = 0
    errors: list[str] = []
    posted_model_uids: set[str] = set()
    posted_pairs: list[tuple[str, str]] = []
    unlisted_pairs: list[tuple[str, str]] = []
    rate_limited_fails = 0
    cancelled = False
    t_post = time.time()

    by_collection: dict[tuple[str, str], list[str]] = defaultdict(list)
    for coll_uid, model_uid, coll_name in ops:
        by_collection[(coll_uid, coll_name)].append(model_uid)

    # One collection at a time so we can verify membership once after its POSTs
    # (Sketchfab returns 201 for age-restricted models without actually adding them).
    collection_items = list(by_collection.items())
    total_ops = len(ops)
    for coll_idx, ((coll_uid, coll_name), uids) in enumerate(collection_items, start=1):
        if _cancelled():
            cancelled = True
            break
        claimed: list[str] = []
        for i in range(0, len(uids), max(1, PUSH_BATCH_SIZE)):
            if _cancelled():
                cancelled = True
                break
            batch_uids = uids[i : i + PUSH_BATCH_SIZE]
            if dry_run:
                processed += len(batch_uids)
                if on_progress:
                    on_progress(processed, total_ops, f"Dry-run preview {processed:,}/{total_ops:,}")
                continue
            batch_ok, batch_errors = _post_batch(client, coll_uid, coll_name, batch_uids)
            processed += len(batch_uids)
            claimed.extend(batch_ok)
            failed += len(batch_uids) - len(batch_ok)
            errors.extend(batch_errors)
            unlisted_pairs.extend(_private_pairs_from_errors(batch_errors))
            for err in batch_errors:
                if "429" in err or "rate limit" in err.lower() or "throttl" in err.lower():
                    rate_limited_fails += 1
            waits = client.throttle_waits - throttle_waits_before
            if on_progress:
                on_progress(
                    processed,
                    total_ops,
                    f"Posted… verifying {coll_name} · {processed:,}/{total_ops:,}"
                    + (f" · throttled {waits}x" if waits else ""),
                )
            if PUSH_BATCH_GAP_SEC > 0 and i + PUSH_BATCH_SIZE < len(uids):
                gap = PUSH_BATCH_GAP_SEC * max(1.0, min(2.0, float(client._get_lane_scale()) * 0.35))
                end = time.time() + gap
                while time.time() < end:
                    if _cancelled():
                        cancelled = True
                        break
                    time.sleep(min(0.25, end - time.time()))
            if cancelled:
                break
        if dry_run or cancelled:
            continue
        if not claimed:
            continue
        if PUSH_VERIFY_AFTER:
            if on_progress:
                on_progress(processed, total_ops, f"Verifying {coll_name} on Sketchfab…")
            verified, missing, members = _verify_in_collection(
                client, coll_uid, claimed, known_members=existing.get(coll_name)
            )
            existing[coll_name] = members
        else:
            # Trust successful POSTs — full re-list of large collections was the Push bottleneck.
            verified = list(claimed)
            missing = []
            prev = existing.get(coll_name) or set()
            existing[coll_name] = prev | set(claimed)
        posted += len(verified)
        accepted_not_added += len(missing)
        failed += len(missing)
        for mu in verified:
            posted_model_uids.add(mu)
            posted_pairs.append((mu, coll_name))
        for mu in missing:
            errors.append(
                f"{mu} → {coll_name}: API returned OK but model not in collection "
                f"(common for age-restricted / NSFW models — add on the website)"
            )
        if on_progress:
            if PUSH_VERIFY_AFTER:
                on_progress(
                    processed,
                    total_ops,
                    f"{coll_name}: verified {len(verified):,}, not added {len(missing):,} "
                    f"· {processed:,}/{total_ops:,} · {format_duration(time.time() - t0)}",
                )
            else:
                on_progress(
                    processed,
                    total_ops,
                    f"{coll_name}: accepted {len(verified):,} "
                    f"· {processed:,}/{total_ops:,} · {format_duration(time.time() - t0)}",
                )

    post_sec = round(time.time() - t_post, 1)
    return {
        "planned": len(ops),
        "posted": posted,
        "already_present": already_present,
        "failed": failed,
        "processed": processed,
        "dry_run": dry_run,
        "cancelled": cancelled,
        "unknown_collections": sorted(unknown_collections),
        "stale_collections": stale_collections,
        "posted_model_uids": sorted(posted_model_uids),
        "posted_pairs": posted_pairs,
        "unlisted_pairs": unlisted_pairs,
        "errors": errors[:40],
        "throttle_waits": client.throttle_waits - throttle_waits_before,
        "collections_checked": len(needed_names) if verify else 0,
        "membership_verified": bool(verify),
        "verify_after": bool(PUSH_VERIFY_AFTER),
        "preload_sec": preload_sec,
        "post_sec": post_sec,
        "batches": sum(
            (len(uids) + max(1, PUSH_BATCH_SIZE) - 1) // max(1, PUSH_BATCH_SIZE)
            for uids in by_collection.values()
        ),
        "rate_limited_fails": rate_limited_fails,
        "accepted_not_added": accepted_not_added,
        "created_collections": created_collections,
        "elapsed_sec": round(time.time() - t0, 1),
        "post_pace": round(float(client._get_lane_scale()), 2),
    }


def _merge_assigned_into_already(already: str, assigned: str) -> str:
    have = [p.strip() for p in str(already or "").split(",") if p.strip()]
    want = [p.strip() for p in str(assigned or "").split(",") if p.strip()]
    if not want:
        return ", ".join(have)
    lower = {h.casefold() for h in have}
    for name in want:
        if name.casefold() not in lower:
            have.append(name)
            lower.add(name.casefold())
    return ", ".join(have)


def _strip_names_from_cell(cell, names: set[str]) -> str:
    want = {n.casefold() for n in names if n}
    if not want:
        return "" if cell is None or (isinstance(cell, float) and pd.isna(cell)) else str(cell)
    parts = []
    raw = "" if cell is None or (isinstance(cell, float) and pd.isna(cell)) else str(cell)
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        if p.casefold() in want:
            continue
        parts.append(p)
    return ", ".join(parts)


def apply_push_markers(
    liked_df: pd.DataFrame,
    posted_model_uids: list[str] | None = None,
    posted_pairs: list[tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """Mark verified posts: Push Sent + fold collection into Already In, clear Assigned.

    Only call with pairs that were confirmed present on Sketchfab after POST.
    """
    if liked_df is None or liked_df.empty:
        return liked_df
    pairs = [(str(u).strip(), str(c or "").strip()) for u, c in (posted_pairs or []) if str(u).strip()]
    if not pairs and posted_model_uids:
        # Legacy uid-only: mark sent, leave Assigned/Already In alone.
        pairs = [(str(u).strip(), "") for u in posted_model_uids if str(u).strip()]
    if not pairs:
        return liked_df

    out = liked_df.copy()
    ucol = uid_col(out)
    if not ucol:
        return out
    for col in ("Push Sent", "Pushed At", "Already In Collection(s)", "Assigned Collection(s)", "Manual"):
        if col not in out.columns:
            out[col] = ""
        else:
            out[col] = out[col].astype(object).where(pd.notna(out[col]), "").astype(str)

    by_uid: dict[str, set[str]] = defaultdict(set)
    uid_only: set[str] = set()
    for uid, coll in pairs:
        if not coll:
            uid_only.add(uid)
            continue
        by_uid[uid].add(coll)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for uid in set(by_uid) | uid_only:
        mask = out[ucol].astype(str) == uid
        if not mask.any():
            continue
        out.loc[mask, "Push Sent"] = "yes"
        out.loc[mask, "Pushed At"] = ts
        colls = by_uid.get(uid)
        if not colls:
            continue
        fold = ", ".join(sorted(colls))
        out.loc[mask, "Already In Collection(s)"] = [
            _merge_assigned_into_already(a, fold) for a in out.loc[mask, "Already In Collection(s)"]
        ]
        out.loc[mask, "Assigned Collection(s)"] = [
            _strip_names_from_cell(a, colls) for a in out.loc[mask, "Assigned Collection(s)"]
        ]
        if "Manual" in out.columns:
            out.loc[mask, "Manual"] = [
                _strip_names_from_cell(a, colls) for a in out.loc[mask, "Manual"]
            ]
        if "Assigned At" in out.columns:
            for idx in out.index[mask]:
                cell = str(out.at[idx, "Assigned Collection(s)"] or "").strip()
                if not cell or cell.lower() in {"nan", "none"}:
                    out.at[idx, "Assigned At"] = ""
    return out


def apply_unlisted_markers(
    liked_df: pd.DataFrame,
    unlisted_pairs: list[tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """Shelf private-of-others failures locally as Unlisted (do not unlike).

    Puts Unlisted + the intended collection name(s) into Already In so the row
    leaves Unassigned/Pending while N/W filters still match those collection labels.
    Clears those names from Assigned. Does not set Push Sent.
    """
    if liked_df is None or liked_df.empty:
        return liked_df
    pairs = [(str(u).strip(), str(c or "").strip()) for u, c in (unlisted_pairs or []) if str(u).strip()]
    if not pairs:
        return liked_df

    out = liked_df.copy()
    ucol = uid_col(out)
    if not ucol:
        return out
    for col in ("Already In Collection(s)", "Assigned Collection(s)", "Manual", "Assignment Notes"):
        if col not in out.columns:
            out[col] = ""
        else:
            out[col] = out[col].astype(object).where(pd.notna(out[col]), "").astype(str)

    by_uid: dict[str, set[str]] = defaultdict(set)
    for uid, coll in pairs:
        by_uid[uid].add(coll if coll else UNLISTED_COLLECTION)

    note = "Unlisted: Sketchfab blocks collecting private models of others"
    for uid, colls in by_uid.items():
        mask = out[ucol].astype(str) == uid
        if not mask.any():
            continue
        fold_names = {UNLISTED_COLLECTION} | {
            c for c in colls if c and c.casefold() != UNLISTED_COLLECTION.casefold()
        }
        fold = ", ".join(
            sorted(fold_names, key=lambda n: (n.casefold() != UNLISTED_COLLECTION.casefold(), n.casefold()))
        )
        out.loc[mask, "Already In Collection(s)"] = [
            _merge_assigned_into_already(a, fold) for a in out.loc[mask, "Already In Collection(s)"]
        ]
        out.loc[mask, "Assigned Collection(s)"] = [
            _strip_names_from_cell(a, colls) for a in out.loc[mask, "Assigned Collection(s)"]
        ]
        if "Manual" in out.columns:
            out.loc[mask, "Manual"] = [
                _strip_names_from_cell(a, colls) for a in out.loc[mask, "Manual"]
            ]
        if "Assignment Notes" in out.columns:
            out.loc[mask, "Assignment Notes"] = [
                _append_note(n, note) for n in out.loc[mask, "Assignment Notes"]
            ]
        if "Assigned At" in out.columns:
            for idx in out.index[mask]:
                cell = str(out.at[idx, "Assigned Collection(s)"] or "").strip()
                if not cell or cell.lower() in {"nan", "none"}:
                    out.at[idx, "Assigned At"] = ""
    return out


def mark_unlisted_inplace(
    liked_df: pd.DataFrame,
    uids: list[str],
    extra_collections: list[str] | None = None,
) -> int:
    """Shelf selected models as Unlisted using Assigned (+ optional extras) as filter labels.

    Returns number of UIDs marked. Mutates liked_df in place.
    """
    if liked_df is None or liked_df.empty:
        return 0
    ucol = uid_col(liked_df)
    if not ucol:
        return 0
    extras = [c.strip() for c in (extra_collections or []) if str(c).strip()]
    pairs: list[tuple[str, str]] = []
    touched: set[str] = set()
    for uid in uids:
        uid = str(uid or "").strip()
        if not uid:
            continue
        mask = liked_df[ucol].astype(str) == uid
        if not mask.any():
            continue
        row = liked_df.loc[mask].iloc[0]
        intended = _csv_parts(row.get(ASSIGNED_COL)) if ASSIGNED_COL in liked_df.columns else []
        for name in intended + extras:
            if name:
                pairs.append((uid, name))
        if not intended and not extras:
            pairs.append((uid, ""))  # Unlisted alone
        touched.add(uid)
    if not pairs:
        return 0
    updated = apply_unlisted_markers(liked_df, pairs)
    for col in ("Already In Collection(s)", "Assigned Collection(s)", "Manual", "Assignment Notes"):
        if col in updated.columns:
            if col not in liked_df.columns:
                liked_df[col] = ""
            liked_df[col] = updated[col].astype(object).to_numpy()
    return len(touched)


def _append_note(cell, note: str) -> str:
    cur = _cell_text(cell)
    if not note:
        return cur
    if note.casefold() in cur.casefold():
        return cur
    return f"{cur}; {note}".strip("; ").strip() if cur else note


def summarize_push_result(res: dict) -> list[str]:
    lines: list[str] = []
    planned = int(res.get("planned") or 0)
    posted = int(res.get("posted") or 0)
    failed = int(res.get("failed") or 0)
    already = int(res.get("already_present") or 0)
    dry = bool(res.get("dry_run"))
    cancelled = bool(res.get("cancelled"))
    throttled = int(res.get("throttle_waits") or 0)
    checked = int(res.get("collections_checked") or 0)
    elapsed = float(res.get("elapsed_sec") or 0)
    rate_fails = int(res.get("rate_limited_fails") or 0)
    pace = res.get("post_pace")
    processed = int(res.get("processed") or (posted + failed))
    ghost = int(res.get("accepted_not_added") or 0)
    unlisted_n = len({u for u, _ in (res.get("unlisted_pairs") or []) if u})

    if planned == 0 and already == 0:
        if res.get("unknown_collections"):
            lines.append(
                "Nothing posted — unknown collection name(s): "
                + ", ".join(res.get("unknown_collections") or [])
                + ". Collect, or type the name in Assign to create on Sketchfab, then Push again."
            )
        else:
            lines.append("Nothing to push — no Assigned rows need syncing to Sketchfab.")
        created = res.get("created_collections") or []
        if created:
            names = ", ".join(c.get("name") or "?" for c in created[:5])
            lines.append(f"Created on Sketchfab: {names}" + ("…" if len(created) > 5 else "") + ".")
        if elapsed:
            lines.append(f"Push check finished in {format_duration(elapsed)}.")
        return lines

    if cancelled:
        lines.append(
            f"Push stopped — posted {posted:,} of {planned:,} planned "
            f"({processed:,} attempted before stop). Remaining stay in Pending."
        )
    elif dry:
        lines.append(f"Dry-run: would add {planned:,} model(s) to Sketchfab collections (no API writes).")
    else:
        if res.get("verify_after"):
            lines.append(f"Push complete — verified {posted:,} of {planned:,} planned assignment(s) on Sketchfab.")
        else:
            lines.append(
                f"Push complete — accepted {posted:,} of {planned:,} planned assignment(s) "
                f"(trusted POST; set PUSH_VERIFY_AFTER=1 to re-list collections)."
            )
        if unlisted_n:
            lines.append(
                f"Shelved {unlisted_n:,} as Unlisted (private models Sketchfab won't collect — like kept). "
                "They leave Pending; N/W filters still apply via their intended collections."
            )
        if ghost:
            lines.append(
                f"API accepted but did NOT add {ghost:,} model(s) (usually age-restricted / NSFW). "
                "They stay Pending — add those on the Sketchfab website; the Data API will not place them."
            )
        other_fail = max(0, failed - ghost - len(res.get("unlisted_pairs") or []))
        if other_fail and not ghost and not unlisted_n:
            lines.append(
                f"Failed: {failed:,}"
                + (f" (~{rate_fails:,} look like rate-limit / 429)" if rate_fails else "")
                + ". Re-Push later — already-posted rows are skipped."
            )
        elif other_fail:
            lines.append(
                f"Other failures: {other_fail:,}"
                + (f" (~{rate_fails:,} rate-limit / 429)" if rate_fails else "")
                + "."
            )

    if elapsed:
        rate = (posted / elapsed) if elapsed > 0 and posted and not dry else 0
        rate_note = f" (~{rate:.1f} posts/s)" if rate else ""
        lines.append(f"Elapsed {format_duration(elapsed)}{rate_note}" + (f" · end pace x{pace}" if pace else "") + ".")
        preload_sec = float(res.get("preload_sec") or 0)
        post_sec = float(res.get("post_sec") or 0)
        batches = int(res.get("batches") or 0)
        parts = []
        if preload_sec:
            parts.append(f"membership check {format_duration(preload_sec)}")
        if post_sec:
            parts.append(f"posting {format_duration(post_sec)}")
        if batches:
            parts.append(f"{batches} API batch(es)")
        if parts:
            lines.append("Breakdown: " + " · ".join(parts) + ".")
        if not res.get("membership_verified") and not dry:
            lines.append(
                "Skipped live membership crawl before posting (trusting Already In). "
                "Use Check sync to verify against Sketchfab, or set PUSH_VERIFY_MEMBERSHIP=1."
            )
        if not res.get("verify_after") and not dry and posted:
            lines.append(
                "Skipped post-verify re-list (fast path). "
                "Rare age-restricted models can look posted but stay off the site — Check sync if unsure."
            )

    if created := res.get("created_collections"):
        names = ", ".join(str(c.get("name") or "?") for c in created[:5])
        lines.append(
            "Created collection(s) on Sketchfab: "
            + names
            + ("…" if len(created) > 5 else "")
            + " (seed model included)."
        )

    if checked:
        lines.append(f"Checked {checked:,} assigned collection(s) on Sketchfab before posting.")

    if already:
        lines.append(f"Already on Sketchfab: {already:,} assigned model/collection pair(s) skipped.")

    if throttled:
        lines.append(
            f"API throttled {throttled:,} time(s); adaptive slowdown applied. "
            "Push defaults stay conservative (~2.2s between POSTs, batch 12, 0.6s batch gap). "
            "Override in .env: MIN_POST_INTERVAL_SEC / PUSH_BATCH_SIZE / PUSH_BATCH_GAP_SEC."
        )

    unknown = res.get("unknown_collections") or []
    if unknown:
        lines.append(
            "Unknown collection name(s): "
            + ", ".join(unknown)
            + ". Run Collect if you created them on sketchfab.com, or type the name in Assign to create via API."
        )

    stale = res.get("stale_collections") or []
    if stale:
        lines.append(
            f"Could not read {len(stale)} stale collection(s) from Sketchfab (404/deleted). "
            "Run Collect to refresh collection UIDs."
        )

    if dry and planned:
        lines.append("Turn Dry-run OFF, then Push again to sync for real.")

    for err in (res.get("errors") or [])[:8]:
        lines.append(f"Error: {err}")

    return lines
