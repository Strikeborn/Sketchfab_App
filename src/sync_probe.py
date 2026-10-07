"""Lightweight Sketchfab ↔ workbook drift checks (counts, recent likes, collection sizes)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from data_io import XL_PATH
from sketchfab_client import SketchfabClient

logger = logging.getLogger(__name__)


@dataclass
class AccountCounts:
    like_count: int | None = None
    collection_count: int | None = None
    subscription_count: int | None = None
    following_count: int | None = None
    username: str = ""
    display_name: str = ""


@dataclass
class CollectionDrift:
    name: str
    uid: str
    api_count: int
    wb_count: int
    listed_count: int | None = None

    @property
    def delta(self) -> int:
        ref = self.listed_count if self.listed_count is not None else self.wb_count
        return int(self.api_count) - int(ref)


@dataclass
class SyncProbeResult:
    account: AccountCounts = field(default_factory=AccountCounts)
    wb_like_count: int = 0
    wb_collection_count: int = 0
    wb_sub_count: int = 0
    new_like_uids: list[str] = field(default_factory=list)
    removed_like_uids: list[str] = field(default_factory=list)
    collection_drifts: list[CollectionDrift] = field(default_factory=list)
    likes_fetched: int = 0
    message: str = ""

    @property
    def like_count_delta(self) -> int | None:
        if self.account.like_count is None:
            return None
        return int(self.account.like_count) - int(self.wb_like_count)


def _wb_collection_counts(colls_df: pd.DataFrame) -> dict[str, tuple[str, int]]:
    """uid -> (name, model count from sheet)."""
    out: dict[str, tuple[str, int]] = {}
    if colls_df is None or colls_df.empty:
        return out
    name_col = "Collection Name" if "Collection Name" in colls_df.columns else ""
    uid_col = "Collection UID" if "Collection UID" in colls_df.columns else ""
    cnt_col = "Model Count" if "Model Count" in colls_df.columns else ""
    if not uid_col:
        return out
    for _, row in colls_df.iterrows():
        uid = str(row.get(uid_col) or "").strip()
        if not uid:
            continue
        name = str(row.get(name_col) or uid).strip()
        try:
            cnt = int(row.get(cnt_col) or 0)
        except (TypeError, ValueError):
            cnt = 0
        out[uid] = (name, cnt)
    return out


def fetch_account_counts(client: SketchfabClient | None = None) -> AccountCounts:
    client = client or SketchfabClient()
    resp = client._request("GET", "/me", max_retries=3, retry_wait_cap=12.0)
    me = resp.json() or {}
    return AccountCounts(
        like_count=int(me.get("likeCount") or 0) if me.get("likeCount") is not None else None,
        collection_count=int(me.get("collectionCount") or 0)
        if me.get("collectionCount") is not None
        else None,
        subscription_count=int(me.get("subscriptionCount") or 0)
        if me.get("subscriptionCount") is not None
        else None,
        following_count=int(me.get("followingCount") or 0)
        if me.get("followingCount") is not None
        else None,
        username=str(me.get("username") or "").strip(),
        display_name=str(me.get("displayName") or me.get("username") or "").strip(),
    )


def list_collections_with_counts(client: SketchfabClient | None = None) -> list[dict]:
    """GET /me/collections — includes modelCount without listing every model."""
    client = client or SketchfabClient()
    out: list[dict] = []
    url: str | None = f"{client.api_base}/me/collections?count=100"
    while url:
        resp = client._request("GET", url, max_retries=3, retry_wait_cap=12.0)
        data = resp.json()
        out.extend(data.get("results") or [])
        url = data.get("next")
    return out


def probe_collection_drifts(
    colls_df: pd.DataFrame,
    *,
    client: SketchfabClient | None = None,
    tolerance: int = 0,
) -> list[CollectionDrift]:
    client = client or SketchfabClient()
    wb = _wb_collection_counts(colls_df)
    drifts: list[CollectionDrift] = []
    try:
        api_cols = list_collections_with_counts(client)
    except Exception as exc:
        logger.warning("Collection list probe failed: %s", exc)
        return drifts
    for c in api_cols:
        uid = str(c.get("uid") or "").strip()
        if not uid:
            continue
        try:
            api_n = int(c.get("modelCount") or 0)
        except (TypeError, ValueError):
            api_n = 0
        name = str(c.get("name") or uid).strip()
        wb_name, wb_n = wb.get(uid, ("", 0))
        if abs(api_n - wb_n) > tolerance:
            drifts.append(
                CollectionDrift(
                    name=wb_name or name,
                    uid=uid,
                    api_count=api_n,
                    wb_count=wb_n,
                )
            )
    # Collections removed on site but still in workbook
    api_uids = {str(c.get("uid") or "").strip() for c in api_cols}
    for uid, (name, wb_n) in wb.items():
        if uid and uid not in api_uids:
            drifts.append(CollectionDrift(name=name, uid=uid, api_count=0, wb_count=wb_n))
    return drifts


def probe_recent_likes(
    liked_df: pd.DataFrame,
    *,
    client: SketchfabClient | None = None,
    max_pages: int = 3,
) -> tuple[list[str], list[str], int]:
    """
    Fetch newest like pages; return (new_uids, removed_uids_guess, pages_fetched).
    Full unlike list requires a full likes fetch when API count < workbook count.
    """
    from collector import get_likes

    client = client or SketchfabClient()
    acct = fetch_account_counts(client)
    wb_uids = set()
    if liked_df is not None and not liked_df.empty:
        uc = "UID" if "UID" in liked_df.columns else ("Model UID" if "Model UID" in liked_df.columns else "")
        if uc:
            wb_uids = {str(u).strip().lower() for u in liked_df[uc].astype(str) if str(u).strip()}

    target = acct.like_count
    need_full = target is not None and len(wb_uids) > 0 and target < len(wb_uids)
    pages = max_pages if not need_full else 9999

    likes = get_likes(
        max_pages=pages,
        target_count=target if need_full else None,
        wb_uid_count=len(wb_uids),
        quiet=True,
    )
    api_uids = [str(m.get("uid") or "").strip().lower() for m in likes if m.get("uid")]
    api_set = set(api_uids)
    new_uids = [u for u in api_uids if u not in wb_uids]
    removed = sorted(wb_uids - api_set) if need_full or (target is not None and len(api_set) >= target) else []
    return new_uids, removed, max(1, (len(likes) + 99) // 100)


def run_sync_probe(
    liked_df: pd.DataFrame,
    colls_df: pd.DataFrame,
    subs_df: pd.DataFrame | None = None,
    *,
    client: SketchfabClient | None = None,
    max_like_pages: int = 3,
    check_collections: bool = True,
    light: bool = False,
) -> SyncProbeResult:
    client = client or SketchfabClient()
    res = SyncProbeResult(
        wb_like_count=len(liked_df) if liked_df is not None else 0,
        wb_collection_count=len(colls_df) if colls_df is not None else 0,
        wb_sub_count=len(subs_df) if subs_df is not None else 0,
    )
    try:
        res.account = fetch_account_counts(client)
    except Exception as exc:
        res.message = f"Account probe failed: {exc}"
        return res

    try:
        like_pages = 0 if light else max_like_pages
        if light and res.account.like_count is not None:
            delta_pre = int(res.account.like_count) - res.wb_like_count
            if delta_pre != 0:
                like_pages = min(5, max(1, max_like_pages))
        if like_pages > 0 or not light:
            new_u, rem_u, npg = probe_recent_likes(
                liked_df, client=client, max_pages=like_pages or max_like_pages
            )
            res.new_like_uids = new_u
            res.removed_like_uids = rem_u
            res.likes_fetched = npg
    except Exception as exc:
        logger.warning("Recent likes probe failed: %s", exc)

    if check_collections and not light:
        try:
            res.collection_drifts = probe_collection_drifts(colls_df, client=client)
        except Exception as exc:
            logger.warning("Collection drift probe failed: %s", exc)

    parts: list[str] = []
    d = res.like_count_delta
    if d is not None:
        if d == 0:
            parts.append(f"likes match ({res.wb_like_count:,})")
        elif d > 0:
            parts.append(f"+{d:,} likes on Sketchfab vs workbook")
        else:
            parts.append(f"{d:,} likes on Sketchfab vs workbook")
    if res.new_like_uids:
        parts.append(f"{len(res.new_like_uids):,} new UID(s) in probe")
    if res.removed_like_uids:
        parts.append(f"{len(res.removed_like_uids):,} unliked on site")
    if res.collection_drifts:
        parts.append(f"{len(res.collection_drifts):,} collection count drift(s)")
    res.message = "; ".join(parts) if parts else "No drift detected"
    return res


def format_probe_summary(probe: SyncProbeResult) -> str:
    bits = [probe.message]
    acct = probe.account
    if acct.username or acct.display_name:
        who = acct.display_name or acct.username
        if acct.username and acct.display_name and acct.username.casefold() != acct.display_name.casefold():
            who = f"{acct.display_name} (@{acct.username})"
        bits.append(f"account {who}")
    if probe.collection_drifts:
        sample = probe.collection_drifts[:3]
        names = ", ".join(f"{d.name} ({d.api_count}≠{d.wb_count})" for d in sample)
        bits.append(f"drift: {names}")
    return " · ".join(bits)


def workbook_exists() -> bool:
    import os

    return os.path.isfile(XL_PATH)
