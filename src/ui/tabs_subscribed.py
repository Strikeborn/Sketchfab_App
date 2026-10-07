import math

import flet as ft
import pandas as pd

from collection_urls import collection_public_url
from content_filter_ctx import ContentFilterFlags, collection_name_visible
from ui.scroll_drag import wrap_middle_drag_scroll

_HDR_BG = "#1e293b"
_HDR_BORDER = "#475569"
_ROW_BORDER = "#334155"
_PANEL = "#0f172a"
_COL_SPACING = 10

_COLS = [
    ("Collection Name", 240),
    ("Owner", 150),
    ("Models", 70),
    ("UID", 280),
]


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"none", "nan", "<na>"} else s


def _header_row() -> ft.Container:
    cells = [
        ft.Container(
            width=w,
            content=ft.Text(label, weight=ft.FontWeight.BOLD, size=12),
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
        )
        for label, w in _COLS
    ]
    return ft.Container(
        content=ft.Row(cells, spacing=_COL_SPACING),
        bgcolor=_HDR_BG,
        border=ft.border.only(bottom=ft.BorderSide(1, _HDR_BORDER)),
    )


def _data_row(row: pd.Series) -> ft.Container:
    name = _fmt(row.get("Collection Name"))
    uid = _fmt(row.get("Collection UID"))
    owner = _fmt(row.get("Owner"))
    profile = _fmt(row.get("Owner Profile"))
    count = _fmt(row.get("Model Count"))
    owner_user = ""
    if profile and "sketchfab.com/" in profile:
        owner_user = profile.rstrip("/").split("sketchfab.com/")[-1].split("/")[0]
    coll_url = collection_public_url(name, uid, username=owner_user) if uid else None

    cells = [
        ft.Container(
            width=240,
            content=ft.TextButton(text=name, url=coll_url) if coll_url else ft.Text(name, size=12),
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
        ),
        ft.Container(
            width=150,
            content=ft.TextButton(text=owner, url=profile) if profile else ft.Text(owner, size=12),
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
        ),
        ft.Container(
            width=70,
            content=ft.Text(count, size=12, text_align=ft.TextAlign.RIGHT),
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
        ),
        ft.Container(
            width=280,
            content=ft.Text(uid, size=11, selectable=True, color=ft.Colors.GREY_400),
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
        ),
    ]
    return ft.Container(
        content=ft.Row(cells, spacing=_COL_SPACING),
        border=ft.border.only(bottom=ft.BorderSide(1, _ROW_BORDER)),
    )


class SubscribedTab(ft.Column):
    def __init__(self):
        super().__init__(expand=True, spacing=6)
        self._full_df = pd.DataFrame()
        self._hide_nsfw = True
        self._hide_female = True
        self._hide_male = True
        self._filter_tf = ft.TextField(
            label="Filter subscriptions",
            hint_text="Collection or owner name…",
            width=280,
            dense=True,
            text_size=12,
            on_submit=lambda e: self._apply_filter(),
        )
        self._summary = ft.Text("", size=12, color=ft.Colors.GREY_400)
        self.pager = ft.Row()
        self.header_bar = ft.Container()
        self.scroller = ft.ListView(expand=True, spacing=0, padding=0)
        self._scroller_wrap = wrap_middle_drag_scroll(self.scroller, expand=True)
        self.controls = [
            ft.Container(
                padding=10,
                bgcolor=_PANEL,
                border_radius=8,
                content=ft.Column(
                    [
                        ft.Text("Subscribed collections", size=13, weight=ft.FontWeight.BOLD),
                        ft.Text(
                            "Curated lists you follow on Sketchfab (not collections you created). "
                            "Re-Collect to refresh.",
                            size=11,
                            color=ft.Colors.GREY_500,
                        ),
                        ft.Row(
                            [
                                self._filter_tf,
                                ft.IconButton(icon=ft.Icons.SEARCH, tooltip="Filter", on_click=lambda e: self._apply_filter()),
                                ft.TextButton("Clear", on_click=lambda e: self._clear_filter()),
                                ft.Container(expand=True),
                                self._summary,
                            ],
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                    spacing=6,
                    tight=True,
                ),
            ),
            self.pager,
            self.header_bar,
            self._scroller_wrap,
        ]

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        self._hide_nsfw = bool(hide_nsfw)
        self._hide_female = bool(hide_female)
        self._hide_male = bool(hide_male)
        if hasattr(self, "_page_idx"):
            self.set_df(self._full_df, self._page_idx, getattr(self, "_page_size", 50))

    def _clear_filter(self) -> None:
        self._filter_tf.value = ""
        self._apply_filter()

    def _apply_filter(self) -> None:
        if hasattr(self, "_page_idx"):
            self.set_df(self._full_df, self._page_idx, self._page_size)

    def _filtered_df(self) -> pd.DataFrame:
        df = self._full_df
        q = (self._filter_tf.value or "").strip().lower()
        if not df.empty and q:
            mask = pd.Series(False, index=df.index)
            for col in ("Collection Name", "Owner"):
                if col in df.columns:
                    mask = mask | df[col].fillna("").astype(str).str.lower().str.contains(q, regex=False)
            df = df.loc[mask]
        if df.empty:
            return df
        flags = ContentFilterFlags(
            hide_nsfw=self._hide_nsfw,
            hide_female=self._hide_female,
            hide_male=self._hide_male,
        )
        if not flags.any_on() or "Collection Name" not in df.columns:
            return df
        keep = [
            i
            for i, name in df["Collection Name"].items()
            if collection_name_visible(str(name or ""), flags)
        ]
        return df.loc[keep]

    def set_df(self, df: pd.DataFrame, page_idx: int = 0, page_size: int = 50):
        self._full_df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        self._page_idx = page_idx
        self._page_size = page_size
        filtered = self._filtered_df()

        total_models = 0
        if not filtered.empty and "Model Count" in filtered.columns:
            try:
                total_models = int(pd.to_numeric(filtered["Model Count"], errors="coerce").fillna(0).sum())
            except Exception:
                pass
        self._summary.value = f"{len(filtered)} subscription(s)  ·  {total_models:,} models total"
        if (
            (self._hide_nsfw or self._hide_female or self._hide_male)
            and not self._full_df.empty
            and len(filtered) < len(self._full_df)
        ):
            hidden = len(self._full_df) - len(filtered)
            # text search may also shrink — only mention when N/W/M is on
            q = (self._filter_tf.value or "").strip()
            if not q:
                self._summary.value += f"  ·  N/W/M hiding {hidden:,}"


        if filtered.empty:
            self.header_bar.content = ft.Text(
                "No subscriptions — click Collect (uses /me/subscriptions).",
                size=12,
                color=ft.Colors.GREY_500,
            )
            self.scroller.controls = []
            try:
                if self.page is not None:
                    self.update()
            except (AssertionError, RuntimeError, Exception):
                pass
            return

        total = len(filtered)
        pages = max(1, math.ceil(total / page_size))
        page_idx = max(0, min(page_idx, pages - 1))
        dfp = filtered.iloc[page_idx * page_size : min((page_idx + 1) * page_size, total)]

        # Keep the same header Container mounted; only swap its content.
        new_header = _header_row()
        self.header_bar.content = new_header.content
        self.header_bar.bgcolor = getattr(new_header, "bgcolor", None)
        self.header_bar.border = getattr(new_header, "border", None)
        self.scroller.controls = [_data_row(row) for _, row in dfp.iterrows()]
        try:
            if self.page is not None:
                self.update()
        except (AssertionError, RuntimeError, Exception):
            pass
        try:
            self.scroller.scroll_to(offset=0, duration=0)
        except Exception:
            pass

    def scroll_to_top(self) -> None:
        try:
            self.scroller.scroll_to(offset=0, duration=200)
        except Exception:
            pass
