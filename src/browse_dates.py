"""Browse upload-date filter helpers for Sketchfab search API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Sketchfab /v3/search?type=models only accepts date=1, 7, or 31 (days).
_API_DATE_DAYS = frozenset({1, 7, 31})

DATE_FILTER_OPTS = [
    ("", "All time"),
    ("7", "Last week"),
    ("31", "Last month"),
    ("90", "Last 3 months"),
    ("180", "Last 6 months"),
    ("365", "Last year"),
    ("yearly", "This calendar year"),
]


def days_since_calendar_year_start() -> int:
    now = datetime.now(timezone.utc)
    jan1 = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    return max(1, (now - jan1).days + 1)


def _parse_published_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def resolve_date_filter(filter_key: str) -> tuple[int | None, datetime | None]:
    """
    Split date filter into API param (1/7/31 only) and optional client cutoff.
    Longer ranges are applied locally using publishedAt after the API returns.
    """
    key = (filter_key or "").strip().lower()
    if not key or key == "all":
        return None, None
    if key == "yearly":
        now = datetime.now(timezone.utc)
        return None, datetime(now.year, 1, 1, tzinfo=timezone.utc)
    try:
        days = int(key)
    except ValueError:
        return None, None
    if days in _API_DATE_DAYS:
        return days, None
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    return None, cutoff


def api_date_days(filter_key: str) -> int | None:
    """Days value safe to pass to Sketchfab search API, or None."""
    api_days, _ = resolve_date_filter(filter_key)
    return api_days


def client_cutoff(filter_key: str) -> datetime | None:
    """UTC cutoff for client-side publishedAt filtering."""
    _, cutoff = resolve_date_filter(filter_key)
    return cutoff


def row_within_cutoff(row: dict, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    pub = _parse_published_at(row.get("publishedAt") or "")
    if pub is None:
        return True
    return pub >= cutoff


def normalize_preset_date_filter(saved: str) -> str:
    """Map legacy preset values (e.g. 30) to API-safe keys."""
    key = (saved or "").strip()
    if key == "30":
        return "31"
    return key
