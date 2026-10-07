"""Liked-date helpers — Sketchfab does not return likedAt; we track order + first-seen."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

LIKED_SORT_OPTS = [
    ("order_asc", "Recently liked"),
    ("order_desc", "Oldest likes first"),
    ("liked_at_desc", "Liked date (newest)"),
    ("liked_at_asc", "Liked date (oldest)"),
    ("likes_desc", "Most likes"),
    ("views_desc", "Most views"),
    ("downloads_desc", "Most downloads"),
    ("faces_desc", "Most triangles"),
    ("name", "Name A–Z"),
]

LIKED_WHEN_OPTS = [
    ("", "All time"),
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("month", "This month"),
]


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"none", "nan", "<na>"}:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def month_options_from_series(series) -> list[tuple[str, str]]:
    """Build (YYYY-MM, label) options from Liked At column."""
    months: set[str] = set()
    for val in series:
        dt = _parse_dt(val)
        if dt:
            months.add(dt.strftime("%Y-%m"))
    out = [("", "Any month")]
    for key in sorted(months, reverse=True):
        try:
            y, m = key.split("-")
            label = datetime(int(y), int(m), 1).strftime("%b %Y")
        except ValueError:
            label = key
        out.append((key, label))
    return out


def row_passes_liked_when(liked_at: object, when_key: str, month_key: str = "") -> bool:
    when = (when_key or "").strip()
    month = (month_key or "").strip()
    dt = _parse_dt(liked_at)
    now = datetime.now(timezone.utc)

    if month:
        if not dt:
            return False
        return dt.strftime("%Y-%m") == month

    if not when:
        return True

    if when == "month":
        if not dt:
            return False
        return dt.year == now.year and dt.month == now.month

    try:
        days = int(when)
    except ValueError:
        return True

    if not dt:
        return False
    return dt >= now - timedelta(days=max(1, days))
