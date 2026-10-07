"""Assign liked models to collections in the local workbook."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from state import uid_col, _coerce_str_series


_SUGGEST_COLS = (
    "Suggested Collection(s)",
    "Auto-Assigned Collection(s)",
    "Fuzzy Match Collection(s)",
)
ASSIGNED_AT_COL = "Assigned At"


def _now_assigned_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _pending_assigned_nonempty(cell) -> bool:
    raw = "" if pd.isna(cell) else str(cell).strip()
    if not raw or raw.lower() in {"nan", "none", "<na>"}:
        return False
    return any(p.strip() for p in raw.split(","))


def _strip_name_from_cell(cell, name: str) -> str:
    target = (name or "").strip().casefold()
    if not target:
        return "" if pd.isna(cell) else str(cell)
    parts: list[str] = []
    raw = "" if pd.isna(cell) else str(cell)
    for part in raw.split(","):
        p = part.split(":")[0].strip() if ":" in part else part.strip()
        if not p:
            continue
        if p.casefold() == target:
            continue
        # Keep original fragment (with score suffix if any)
        frag = part.strip()
        if frag and frag not in parts:
            parts.append(frag)
    return ", ".join(parts)


def _merge_into_cell(cell, collection: str) -> str:
    """Append collection to a comma cell, de-duplicated (case-insensitive), order preserved."""
    have = [] if pd.isna(cell) else [p.strip() for p in str(cell).split(",") if p.strip()]
    lower = {h.casefold() for h in have}
    if collection.casefold() not in lower:
        have.append(collection)
    return ", ".join(have)


def _ensure_str_col(df: pd.DataFrame, col: str) -> None:
    if col not in df.columns:
        df[col] = ""
    else:
        df[col] = _coerce_str_series(df[col])


def assign_models_inplace(
    liked_df: pd.DataFrame,
    uids: list[str],
    collection_name: str,
    use_manual: bool = True,
    mode: str = "add",
) -> int:
    """Patch Assigned/Manual on matching rows without copying the whole frame. Returns rows touched.

    mode="add" merges the collection into any existing assignments (multi-collection);
    mode="replace" overwrites the cell with just this collection.
    """
    if liked_df is None or liked_df.empty or not collection_name.strip():
        return 0
    uc = uid_col(liked_df)
    if not uc:
        return 0
    want = {str(u).strip().lower() for u in uids if u}
    if not want:
        return 0
    coll = collection_name.strip()
    mask = liked_df[uc].astype(str).str.strip().str.lower().isin(want)
    n = int(mask.sum())
    if not n:
        return 0
    _ensure_str_col(liked_df, "Assigned Collection(s)")
    if mode == "add":
        liked_df.loc[mask, "Assigned Collection(s)"] = [
            _merge_into_cell(v, coll) for v in liked_df.loc[mask, "Assigned Collection(s)"]
        ]
    else:
        liked_df.loc[mask, "Assigned Collection(s)"] = coll
    if use_manual:
        _ensure_str_col(liked_df, "Manual")
        if mode == "add":
            liked_df.loc[mask, "Manual"] = [
                _merge_into_cell(v, coll) for v in liked_df.loc[mask, "Manual"]
            ]
        else:
            liked_df.loc[mask, "Manual"] = coll
    # Stamp when the local queue entry was set (Pending Push sorts on this).
    _ensure_str_col(liked_df, ASSIGNED_AT_COL)
    liked_df.loc[mask, ASSIGNED_AT_COL] = _now_assigned_at()
    # Drop accepted suggestion chips so they don't reappear after refresh.
    for col in _SUGGEST_COLS:
        if col not in liked_df.columns:
            continue
        _ensure_str_col(liked_df, col)
        liked_df.loc[mask, col] = [
            _strip_name_from_cell(v, coll) for v in liked_df.loc[mask, col]
        ]
    return n


def clear_assignment_inplace(
    liked_df: pd.DataFrame,
    uids: list[str],
    collection: str | None = None,
) -> int:
    """Clear Assigned/Manual so a false queue entry won't Push. Returns rows touched.

    If ``collection`` is given, only that name is removed (leaving other pending
    collections intact); otherwise the whole assignment is cleared.
    """
    if liked_df is None or liked_df.empty:
        return 0
    uc = uid_col(liked_df)
    if not uc:
        return 0
    want = {str(u).strip().lower() for u in uids if u}
    if not want:
        return 0
    mask = liked_df[uc].astype(str).str.strip().str.lower().isin(want)
    n = int(mask.sum())
    if not n:
        return 0
    for col in ("Assigned Collection(s)", "Manual"):
        _ensure_str_col(liked_df, col)
        if collection:
            liked_df.loc[mask, col] = [
                _strip_name_from_cell(v, collection) for v in liked_df.loc[mask, col]
            ]
        else:
            liked_df.loc[mask, col] = ""
    # Clear Assigned At when nothing remains pending on the row.
    if ASSIGNED_AT_COL in liked_df.columns and "Assigned Collection(s)" in liked_df.columns:
        still = liked_df.loc[mask, "Assigned Collection(s)"].map(_pending_assigned_nonempty)
        clear_at = mask & ~still
        if clear_at.any():
            liked_df.loc[clear_at, ASSIGNED_AT_COL] = ""
    return n


def clear_assignment_pairs_inplace(
    liked_df: pd.DataFrame,
    pairs: list[tuple[str, str]],
) -> int:
    """Clear many (uid, collection) pending assignments in one pass. Returns pairs cleared."""
    if liked_df is None or liked_df.empty or not pairs:
        return 0
    by_coll: dict[str, list[str]] = {}
    for uid, coll in pairs:
        u = str(uid or "").strip()
        c = str(coll or "").strip()
        if not u or not c:
            continue
        by_coll.setdefault(c, []).append(u)
    if not by_coll:
        return 0
    cleared = 0
    for coll, uids in by_coll.items():
        # Deduplicate UIDs per collection
        uniq = list(dict.fromkeys(uids))
        n = clear_assignment_inplace(liked_df, uniq, collection=coll)
        if n:
            cleared += len(uniq)  # each uid×coll pair intended
    return cleared


def strip_already_in_inplace(
    liked_df: pd.DataFrame,
    uids: list[str],
    collection: str,
) -> int:
    """Remove a collection name from Already In (after Sketchfab DELETE). Returns rows touched."""
    if liked_df is None or liked_df.empty or not (collection or "").strip():
        return 0
    uc = uid_col(liked_df)
    if not uc:
        return 0
    want = {str(u).strip().lower() for u in uids if u}
    if not want:
        return 0
    col = "Already In Collection(s)"
    _ensure_str_col(liked_df, col)
    mask = liked_df[uc].astype(str).str.strip().str.lower().isin(want)
    n = int(mask.sum())
    if not n:
        return 0
    liked_df.loc[mask, col] = [
        _strip_name_from_cell(v, collection) for v in liked_df.loc[mask, col]
    ]
    return n


def assign_models(
    liked_df: pd.DataFrame,
    uids: list[str],
    collection_name: str,
    use_manual: bool = True,
) -> pd.DataFrame:
    if liked_df is None or liked_df.empty or not collection_name.strip():
        return liked_df
    out = liked_df.copy()
    assign_models_inplace(out, uids, collection_name, use_manual=use_manual)
    # Normalize only when callers expect clean string series (bulk / workbook paths).
    uc = uid_col(out)
    if uc and "Assigned Collection(s)" in out.columns:
        out["Assigned Collection(s)"] = _coerce_str_series(out["Assigned Collection(s)"])
    if use_manual and "Manual" in out.columns:
        out["Manual"] = _coerce_str_series(out["Manual"])
    return out
