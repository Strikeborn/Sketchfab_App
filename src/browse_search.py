"""Browse search helpers — match estimates and paging."""
from __future__ import annotations

from browse_dates import client_cutoff, row_within_cutoff

# Cap background scans so broad searches stay responsive.
ESTIMATE_MAX_PAGES = 8


def estimate_api_matches(
    client,
    params: dict,
    date_filter_key: str,
    *,
    max_pages: int = ESTIMATE_MAX_PAGES,
) -> tuple[int, bool]:
    """
    Walk Sketchfab search pages and count models matching API params + client date cutoff.
    Returns (count, truncated) where truncated=True if more pages exist beyond max_pages.
    """
    cutoff = client_cutoff(date_filter_key)
    total = 0
    truncated = False
    next_url: str | None = None
    for page_idx in range(max_pages):
        if page_idx == 0:
            data = client.search_models(**params)
        else:
            data = client.search_models(cursor_url=next_url)
        for model in data.get("results") or []:
            if row_within_cutoff({"publishedAt": model.get("publishedAt") or ""}, cutoff):
                total += 1
        next_url = data.get("next")
        if not next_url:
            break
    else:
        if next_url:
            truncated = True
    return total, truncated
