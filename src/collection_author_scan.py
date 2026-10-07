"""Scan public (and local) Sketchfab collections for models by a deleted/live author."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from browse_collections import normalize_collection, unwrap_collection_model
from browse_models import normalize_search_model
from model_resolve import author_matches, normalize_name
from series_like import (
    broad_variant_search_queries,
    collect_variant_hits_broad,
    wanted_variant_norms,
)
from sketchfab_client import SketchfabClient

DEFAULT_COLLECTION_QUERIES = (
    "Artist",
    "Artist",
    "Series Prefix",
    "Series",
)

_MAX_BROAD_QUERIES = 45
_MAX_PAGES_PER_BROAD = 3
_SERIES_TITLE = re.compile(r"_T\d|_\d|0", re.I)


@dataclass
class AuthorScanResult:
    author: str
    collections_searched: int = 0
    collections_scanned: int = 0
    models_found: int = 0
    models: list[dict] = field(default_factory=list)
    collection_hits: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    title_variants_searched: int = 0
    title_variants_added: int = 0


def author_aliases(author: str, extra: list[str] | None = None) -> set[str]:
    keys: set[str] = set()
    for raw in [author, *(extra or [])]:
        s = (raw or "").strip()
        if s:
            keys.add(normalize_name(s))
    return keys


def _model_matches(model: dict, keys: set[str]) -> bool:
    for k in keys:
        if author_matches(model, k):
            return True
    user = model.get("user") or {}
    blob = " ".join(
        str(user.get(x) or "") for x in ("username", "displayName", "profileUrl")
    )
    nb = normalize_name(blob)
    for k in keys:
        if k and k in nb:
            return True
    return False


def discover_collections(
    client: SketchfabClient,
    queries: list[str],
    *,
    max_pages_per_query: int = 4,
    max_collections: int = 60,
) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        cursor = None
        for _ in range(max(1, max_pages_per_query)):
            try:
                data = client.search_collections(
                    query=q,
                    sort_by="-subscriberCount",
                    count=24,
                    cursor_url=cursor,
                )
            except Exception:
                break
            for item in data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                row = normalize_collection(item)
                uid = str(row.get("UID") or "").strip()
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                out.append(row)
                if len(out) >= max_collections:
                    return out
            cursor = data.get("next")
            if not cursor:
                break
    return out


def list_subscribed_collection_uids(client: SketchfabClient, *, max_pages: int = 40) -> list[str]:
    uids: list[str] = []
    cursor = None
    for _ in range(max(1, max_pages)):
        try:
            data = client.list_my_subscriptions(count=24, cursor_url=cursor)
        except Exception:
            break
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            nested = item.get("collection")
            c = nested if isinstance(nested, dict) else item
            uid = str(c.get("uid") or "").strip()
            if uid:
                uids.append(uid)
        cursor = data.get("next")
        if not cursor:
            break
    return uids


def scan_collection_for_author(
    client: SketchfabClient,
    collection_uid: str,
    keys: set[str],
    *,
    max_model_pages: int = 15,
) -> list[dict]:
    hits: list[dict] = []
    cursor = None
    for _ in range(max(1, max_model_pages)):
        try:
            data = client.list_collection_models(
                collection_uid,
                count=100,
                cursor_url=cursor,
            )
        except Exception:
            break
        for item in data.get("results") or []:
            model = unwrap_collection_model(item)
            if not isinstance(model, dict):
                continue
            if _model_matches(model, keys):
                hits.append(normalize_search_model(model))
        cursor = data.get("next")
        if not cursor:
            break
    return hits


def series_seed_titles(models: list[dict]) -> list[str]:
    """Series-style titles: underscores and/or T0–T10 / 0–10 suffixes."""
    seeds: list[str] = []
    seen: set[str] = set()
    for m in models:
        name = str(m.get("Name") or "").strip()
        if not name:
            continue
        is_series = bool(
            ("_" in name and (_SERIES_TITLE.search(name) or any(ch.isdigit() for ch in name)))
            or re.search(r"[\s_]T\d+\b", name, re.I)
            or re.search(r"[\s_]\d{1,2}$", name)
            or re.search(r"T\d+$", name, re.I)
        )
        if not is_series:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        seeds.append(name)
    return seeds


def expand_author_title_variants(
    client: SketchfabClient,
    author: str,
    models: list[dict],
    keys: set[str],
    *,
    extra_family_names: list[str] | None = None,
    t_max: int = 10,
    on_progress=None,
    should_cancel=None,
    pause_s: float = 0.12,
) -> tuple[list[dict], int]:
    """
    From collection scan hits, match T0–T10 / 0–10 / no-underscore title variants
    via a small set of broad catalog searches (not one API call per candidate).
    Returns (new model rows, pattern count checked locally).
    """
    existing_uids = {str(m.get("UID") or "").strip() for m in models if str(m.get("UID") or "").strip()}
    existing_names = [str(m.get("Name") or "").strip() for m in models if str(m.get("Name") or "").strip()]
    existing_norms = {normalize_name(n) for n in existing_names if n}
    family = list(dict.fromkeys(existing_names + list(extra_family_names or [])))

    seeds = series_seed_titles(models)
    if not seeds:
        seeds = [
            n for n in existing_names
            if any(ch.isdigit() for ch in n) or re.search(r"T\d", n, re.I)
        ]

    if not seeds:
        return [], 0

    wanted = wanted_variant_norms(
        seeds,
        family,
        t_max=t_max,
        expand_compass_family=True,
        expand_generic_family=True,
    )
    wanted -= existing_norms
    if not wanted:
        return [], 0

    queries = broad_variant_search_queries(seeds, family, author)[:_MAX_BROAD_QUERIES]

    raw_hits, pages = collect_variant_hits_broad(
        client,
        queries,
        keys,
        wanted,
        existing_uids=existing_uids,
        existing_norms=existing_norms,
        author=author,
        max_pages_per_query=_MAX_PAGES_PER_BROAD,
        on_progress=on_progress,
        should_cancel=should_cancel,
        pause_s=pause_s,
    )

    new_rows: list[dict] = []
    for m in raw_hits:
        row = normalize_search_model(m)
        uid = str(row.get("UID") or "").strip()
        if uid and uid not in existing_uids:
            existing_uids.add(uid)
            row["_from_collection"] = "title variant"
            new_rows.append(row)

    # Report pattern count (local), not per-title API calls
    return new_rows, len(wanted)


def scan_collections_for_author(
    client: SketchfabClient,
    author: str,
    *,
    collection_queries: list[str] | None = None,
    extra_collection_uids: list[str] | None = None,
    author_aliases_extra: list[str] | None = None,
    max_collections: int = 50,
    max_pages_per_query: int = 4,
    include_subscriptions: bool = True,
    expand_title_variants: bool = True,
    extra_family_names: list[str] | None = None,
    on_progress=None,
    should_cancel=None,
    pause_s: float = 0.08,
) -> AuthorScanResult:
    """
    Search public collections on Sketchfab, open each, keep models whose owner
    matches author (works when the author profile is deleted but models remain public).
    """
    keys = author_aliases(author, author_aliases_extra)
    if not keys:
        raise ValueError("author required")

    queries = [q.strip() for q in (collection_queries or DEFAULT_COLLECTION_QUERIES) if q.strip()]
    if author.strip() and normalize_name(author) not in {normalize_name(q) for q in queries}:
        queries.insert(0, author.strip())

    res = AuthorScanResult(author=author.strip())
    collections = discover_collections(
        client,
        queries,
        max_pages_per_query=max_pages_per_query,
        max_collections=max_collections,
    )
    res.collections_searched = len(queries)

    uid_to_meta: dict[str, dict] = {}
    for c in collections:
        uid = str(c.get("UID") or "").strip()
        if uid:
            uid_to_meta[uid] = c

    for uid in extra_collection_uids or []:
        uid = str(uid or "").strip()
        if uid and uid not in uid_to_meta:
            uid_to_meta[uid] = {"UID": uid, "Name": f"Collection {uid[:8]}…"}

    if include_subscriptions:
        for uid in list_subscribed_collection_uids(client):
            if uid not in uid_to_meta:
                uid_to_meta[uid] = {"UID": uid, "Name": f"Subscribed {uid[:8]}…"}

    seen_models: set[str] = set()
    total = len(uid_to_meta)

    for i, (cuid, meta) in enumerate(uid_to_meta.items(), start=1):
        if should_cancel and should_cancel():
            break
        if on_progress:
            try:
                label = str(meta.get("Name") or cuid)[:40]
                on_progress(i, total, f"Scan collection {i}/{total}: {label}")
            except Exception:
                pass
        try:
            batch = scan_collection_for_author(client, cuid, keys)
        except Exception as exc:
            res.errors.append(f"{cuid}: {exc}")
            batch = []
        res.collections_scanned += 1
        if batch:
            res.collection_hits.append({**meta, "match_count": len(batch)})
            for row in batch:
                muid = str(row.get("UID") or "").strip()
                if not muid or muid in seen_models:
                    continue
                seen_models.add(muid)
                row["_from_collection"] = meta.get("Name") or cuid
                res.models.append(row)
        if pause_s > 0:
            time.sleep(pause_s)

    if expand_title_variants and res.models:
        n_seeds = len(series_seed_titles(res.models))
        if on_progress:
            try:
                on_progress(0, 0, f"Broad title-variant search ({n_seeds} seeds, ~{min(_MAX_BROAD_QUERIES, 40)} queries)…")
            except Exception:
                pass
        try:
            added, searched = expand_author_title_variants(
                client,
                author.strip(),
                res.models,
                keys,
                extra_family_names=extra_family_names,
                on_progress=on_progress,
                should_cancel=should_cancel,
                pause_s=pause_s,
            )
            res.title_variants_searched = searched
            res.title_variants_added = len(added)
            for row in added:
                res.models.append(row)
        except Exception as exc:
            res.errors.append(f"title variants: {exc}")

    res.models_found = len(res.models)
    return res
