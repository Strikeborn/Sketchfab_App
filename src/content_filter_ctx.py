"""Shared N/W/M content-filter flags + per-surface kits (view-only hide)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

import pandas as pd

from content_filter import filter_collections_df, filter_dataframe, filter_rows, passes_content_filter

# What each surface is allowed to show for *local* filters (not N/W/M).
# N/W/M always comes from the toolbar and applies everywhere listed in SURFACES_NWM.
FILTER_KITS: dict[str, frozenset[str]] = {
    "liked": frozenset({
        "search", "author", "category", "tag_chips", "collection_chips",
        "copyright", "status", "like_sort", "liked_when", "liked_month",
        "downloadable", "on_disk", "has_original",
    }),
    "pending": frozenset({"sort_assigned"}),
    "browse": frozenset({
        "api_query", "api_sort", "category", "downloadable", "animated",
        "staffpicked", "date", "hide_liked",
    }),
    "account": frozenset({"username", "section", "hide_liked"}),
    "open_collection": frozenset({"api_sort", "liked_filter", "downloadable"}),
    "browse_collections": frozenset({"api_query", "by_user", "api_sort"}),
    "my_collections": frozenset({"search_name"}),
    "subscribed": frozenset({"search_name"}),
    "report": frozenset({"term_filter", "assign_coll", "bulk_table"}),
}

# Surfaces that must honor toolbar N/W/M (hide only — never mutate workbook / Push queue).
SURFACES_NWM: tuple[str, ...] = (
    "liked",
    "pending",
    "browse",
    "account",
    "open_collection",
    "browse_collections",
    "my_collections",
    "subscribed",
    "report",
)


@dataclass(frozen=True)
class ContentFilterFlags:
    hide_nsfw: bool = True
    hide_female: bool = True
    hide_male: bool = True

    def as_kwargs(self) -> dict[str, bool]:
        return {
            "hide_nsfw": self.hide_nsfw,
            "hide_female": self.hide_female,
            "hide_male": self.hide_male,
        }

    def any_on(self) -> bool:
        return self.hide_nsfw or self.hide_female or self.hide_male

    def label(self) -> str:
        bits = []
        if self.hide_nsfw:
            bits.append("N")
        if self.hide_female:
            bits.append("W")
        if self.hide_male:
            bits.append("M")
        return "+".join(bits) if bits else "off"


def flags_from_state(state: Any) -> ContentFilterFlags:
    return ContentFilterFlags(
        hide_nsfw=bool(getattr(state, "hide_nsfw", True)),
        hide_female=bool(getattr(state, "hide_female", True)),
        hide_male=bool(getattr(state, "hide_male", True)),
    )


def apply_df(df: pd.DataFrame | None, flags: ContentFilterFlags) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    return filter_dataframe(df, **flags.as_kwargs())


def apply_rows(rows: list[dict], flags: ContentFilterFlags) -> list[dict]:
    return filter_rows(rows, **flags.as_kwargs())


def apply_collections_df(df: pd.DataFrame | None, flags: ContentFilterFlags) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    return filter_collections_df(df, **flags.as_kwargs())


def collection_name_visible(name: str, flags: ContentFilterFlags) -> bool:
    probe = {
        "Name": name or "",
        "Tags": name or "",
        "Categories": "",
        "Assigned Collection(s)": name or "",
    }
    return passes_content_filter(probe, **flags.as_kwargs())


def broadcast_content_filters(
    flags: ContentFilterFlags,
    targets: Iterable[Any],
    *,
    on_error: Callable[[str, BaseException], None] | None = None,
) -> None:
    """Call set_content_filters(**flags) on each target that supports it."""
    kw = flags.as_kwargs()
    for t in targets:
        if t is None:
            continue
        fn = getattr(t, "set_content_filters", None)
        if not callable(fn):
            continue
        try:
            fn(**kw)
        except Exception as e:
            if on_error:
                on_error(type(t).__name__, e)


def kit_allows(surface: str, control: str) -> bool:
    kit = FILTER_KITS.get(surface)
    if kit is None:
        return True
    return control in kit
