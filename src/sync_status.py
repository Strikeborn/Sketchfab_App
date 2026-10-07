"""Local sync heuristics — how fresh is the workbook vs Sketchfab?"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

# Warn once the newest Collect is older than this (website likes/collections drift).
STALE_COLLECT_HOURS = 12.0

_FMTS = ("%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _parse_ts(val) -> datetime | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s:
        return None
    for fmt in _FMTS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return pd.to_datetime(s, utc=True).to_pydatetime()
    except Exception:
        return None


def _latest(df: pd.DataFrame, col: str) -> datetime | None:
    if df is None or getattr(df, "empty", True) or col not in df.columns:
        return None
    best: datetime | None = None
    for val in df[col]:
        ts = _parse_ts(val)
        if ts and (best is None or ts > best):
            best = ts
    return best


def format_duration(seconds: float | int | None) -> str:
    """Human elapsed: 45s · 3m 20s · 1h 04m · 2d 3h."""
    if seconds is None:
        return "—"
    secs = max(0, int(round(float(seconds))))
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {s:02d}s" if s else f"{mins}m"
    hours, m = divmod(mins, 60)
    if hours < 48:
        return f"{hours}h {m:02d}m" if m else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h" if h else f"{days}d"


def humanize_ago(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    delta = datetime.now(timezone.utc) - ts
    secs = max(0, int(delta.total_seconds()))
    if secs < 10:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    rem_s = secs % 60
    if mins < 60:
        return f"{mins}m {rem_s}s ago" if rem_s and mins < 10 else f"{mins}m ago"
    hours = mins // 60
    rem_m = mins % 60
    if hours < 48:
        return f"{hours}h {rem_m}m ago" if rem_m else f"{hours}h ago"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h ago" if rem_h else f"{days}d ago"


def collected_ago_text(liked_df: pd.DataFrame) -> str:
    return f"Collected {humanize_ago(_latest(liked_df, 'Collected At'))}"


def local_pending_counts(liked_df: pd.DataFrame) -> tuple[int, int]:
    """(pending_models, pending_assignment_pairs) from Assigned ∉ Already In."""
    try:
        from push_assignments import pending_push_rows

        rows = pending_push_rows(liked_df)
    except Exception:
        return 0, 0
    models = len({r["uid"] for r in rows})
    return models, len(rows)


def local_sync_summary(liked_df: pd.DataFrame) -> tuple[str, bool]:
    """Returns (text, warn) describing Collect / Push freshness and local pending queue."""
    collected = _latest(liked_df, "Collected At")
    pushed = _latest(liked_df, "Pushed At")
    models, pairs = local_pending_counts(liked_df)
    parts = [
        f"Collected {humanize_ago(collected)}",
        f"Pushed {humanize_ago(pushed)}",
    ]
    if pairs:
        if pairs == models:
            parts.append(f"· local queue {models:,} pending")
        else:
            parts.append(f"· local queue {models:,} models / {pairs:,} assignments")
    else:
        parts.append("· local queue empty")
    warn = False
    if collected is not None:
        age_h = (datetime.now(timezone.utc) - collected).total_seconds() / 3600.0
        if age_h >= STALE_COLLECT_HOURS:
            warn = True
            parts.append("· workbook may be stale — run Collect to catch website changes")
    else:
        warn = True
        parts.append("· never collected — run Collect")
    return "  ".join(parts), warn
