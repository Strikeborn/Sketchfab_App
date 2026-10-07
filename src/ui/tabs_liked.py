from __future__ import annotations

import threading

import flet as ft
import pandas as pd

from liked_dates import LIKED_SORT_OPTS, LIKED_WHEN_OPTS, month_options_from_series
from search_filter import (
    filter_liked_df,
    list_authors,
    list_categories,
    list_collection_names,
    list_licenses,
    list_tags,
    normalize_license_label,
)
from content_filter import filter_dataframe, collection_hidden_by_filters
from quick_assign import resolve_collection
from collection_match import match_collection_name
from ui.flet_safe import safe_update
from ui.thumb_lod import make_lod_thumb, thumb_urls_from_row, ViewportLod, ScrollLodDebouncer
from ui.scroll_drag import wrap_middle_drag_scroll
from ui.viewer_place import screen_rect_for_card_image
from ui.liked_style import (  # noqa: F401 — re-export names used throughout LikedTab
    download_icon_style,
    row_is_downloadable,
    _PANEL,
    _HDR_BG,
    _HDR_BORDER,
    _ROW_BORDER,
    _ROW_ALT,
    _CHIP_BG,
    _CHIP_HOVER,
    _MIN_COLS,
    _MAX_COLS,
    _DEFAULT_COLS,
    _MIN_CARD_W,
    _CHIP_CAP,
    _CHIP_H,
    _CHIP_H_DENSE,
    _CHIP_ZONE_H,
    _ACTIONS_ZONE_H,
    _SCROLL_LOAD_THRESHOLD,
    _LICENSE_COLOR,
    _GRID_SIDE_CHROME,
    _GRID_GAP,
    _FALLBACK_GRID_AVAIL,
    _STATUS_PENDING,
    _STATUS_IN,
    _STATUS_UNLISTED,
    _BG_GRID,
    _BG_PENDING,
    _BG_IN,
    _BG_PENDING_IN,
    _STATUS_PENDING_SCRIM,
    _STATUS_IN_SCRIM,
    _STATUS_NEUTRAL_SCRIM,
    _RAIL_W,
    _META_ON_SCRIM,
    _LICENSE_ON_SCRIM,
    _LIST_COLS,
    _COL_WIDTHS,
    _ACTIONS_W,
    _SEL_COL_W,
    _HDR_LABELS,
    _COL_SPACING,
    _ROW_BATCH,
    _fmt,
    _uid,
    _name,
    _url,
    _yes_icon,
    _csv_names,
    _row_collection_status,
    _already_tooltip,
    _status_border,
    _status_tint60,
    _status_bg,
    _prefetch,
)
import perf_log
import time as _time


class LikedTab(ft.Column):
    def __init__(self, page: ft.Page | None = None):
        super().__init__(expand=True, spacing=6)
        self._page = page
        self._source_df = pd.DataFrame()
        self._colls_df = pd.DataFrame()
        self._filtered = pd.DataFrame()
        self._page_slice = pd.DataFrame()
        self._load_token = 0
        self._lod = ViewportLod()
        self._lod_debouncer: ScrollLodDebouncer | None = None
        self._view_mode = "grid"
        self._grid_cols = _DEFAULT_COLS
        self._grid_avail_w = 0
        self._last_card_w = 0
        self._relayout_gen = 0
        self._on_filter_change = None
        self._on_assign = None
        self._on_bulk_assign = None
        self._on_assign_create = None
        self._on_remove_from_collection = None
        self._on_mark_unlisted = None
        self._on_preview = None
        self._on_download = None
        self._on_open_account = None
        self._on_series_like = None
        self._on_dismiss = None
        self._hide_nsfw = True
        self._hide_female = True
        self._hide_male = True
        self._selected_uids: set[str] = set()
        self._sel_anchor: str | None = None
        self._sel_cb_by_uid: dict[str, ft.Checkbox] = {}
        self._page_size = 50
        self._display_limit = 50
        self._loading_more = False
        self._card_by_uid: dict[str, ft.Control] = {}
        self._disk_icon_by_uid: dict[str, ft.Icon] = {}
        self._hidden_uids: set[str] = set()  # dimmed cards — layout slot kept (no reflow)
        self._rendered_visible = 0
        self._consumed_suggestions: dict[str, set[str]] = {}
        self._chip_by_key: dict[tuple[str, str], ft.Control] = {}
        self._assigned_label_by_uid: dict[str, ft.Text] = {}
        self._status_badge_by_uid: dict[str, ft.Container] = {}
        self._status_rail_by_uid: dict[str, list[ft.Container]] = {}
        self._already_chips_by_uid: dict[str, ft.Row] = {}
        self._pending_by_uid: dict[str, str] = {}
        self._qa_targets: dict[str, str] = {}
        self._scroll_offset = 0.0
        self._viewport_h = 800.0
        self._rect_by_uid: dict[str, dict[str, int]] = {}
        self._on_selection_change = None
        self._suppress_assign_change = False
        self._text_focus = False
        self._auto_scroll = False
        self._filter_debounce_gen = 0
        self._auto_speed = 480.0  # px/s
        self._auto_task_running = False
        self._max_scroll_extent = 0.0

        self._page_select = ft.Checkbox(
            label="Page",
            value=False,
            tooltip="Select all on this page",
            on_change=lambda e: self._on_page_select_toggle(e),
        )
        self._sel_label = ft.Text("", size=11, color=ft.Colors.GREY_400)
        self._clear_sel_btn = ft.TextButton("Clear sel", on_click=lambda e: self.clear_selection())
        self._series_btn = ft.IconButton(
            icon=ft.Icons.AUTO_FIX_HIGH,
            tooltip="Series discover — search T0–T10 / # variants and like matches (uses author from selection)",
            on_click=lambda e: self._click_series_like(),
        )

        self._query = ft.TextField(
            label="Search",
            hint_text="Name, tags, author, UID…",
            width=150,
            dense=True,
            text_size=12,
            on_submit=lambda e: self._apply_filters(immediate=True),
            on_focus=lambda e: self._set_text_focus(True),
            on_blur=lambda e: self._set_text_focus(False),
        )
        _dd = dict(dense=True, text_size=12, content_padding=ft.padding.symmetric(horizontal=8, vertical=4))
        self._author_dd = ft.Dropdown(label="Author", width=220, value="(all)", options=[ft.dropdown.Option("(all)")], **_dd)
        self._category_dd = ft.Dropdown(label="Category", width=200, value="(all)", options=[ft.dropdown.Option("(all)")], **_dd)
        self._tag_dd = ft.Dropdown(
            label="Tag",
            width=200,
            value="(all)",
            options=[ft.dropdown.Option("(all)")],
            tooltip="Pick a tag to add — chips below; use AND/OR beside them",
            on_change=lambda e: self._add_filter_tag(),
            **_dd,
        )
        self._tag_mode_seg = ft.SegmentedButton(
            selected={"and"},
            segments=[
                ft.Segment(value="and", label=ft.Text("AND")),
                ft.Segment(value="or", label=ft.Text("OR")),
            ],
            on_change=lambda e: self._on_tag_mode(),
        )
        self._coll_filter_dd = ft.Dropdown(
            label="Collection",
            width=260,
            value="(all)",
            options=[ft.dropdown.Option("(all)")],
            tooltip=(
                "Exact collection name in Assigned or Already In (not Suggested). "
                "Female ≠ Female reference. Chips combine with OR."
            ),
            on_change=lambda e: self._add_filter_coll(),
            **_dd,
        )
        self._license_dd = ft.Dropdown(
            label="Copyright",
            width=220,
            value="(all)",
            options=[ft.dropdown.Option("(all)", "Any copyright")],
            **_dd,
        )
        self._status_dd = ft.Dropdown(
            label="Status",
            width=200,
            value="all",
            options=[
                ft.dropdown.Option("all", "All"),
                ft.dropdown.Option("not_in_collection", "Not in collection"),
                ft.dropdown.Option("unassigned", "Unassigned"),
                ft.dropdown.Option("assigned", "Assigned / pending"),
                ft.dropdown.Option("in_collection", "In collection"),
                ft.dropdown.Option("unlisted", "Unlisted (private)"),
            ],
            **_dd,
        )
        self._liked_sort_dd = ft.Dropdown(
            label="Like sort",
            width=170,
            value="order_asc",
            options=[ft.dropdown.Option(v, t) for v, t in LIKED_SORT_OPTS],
            **_dd,
        )
        self._liked_when_dd = ft.Dropdown(
            label="Liked when",
            width=140,
            value="",
            options=[ft.dropdown.Option(v, t) for v, t in LIKED_WHEN_OPTS],
            **_dd,
        )
        self._liked_month_dd = ft.Dropdown(
            label="Month",
            width=130,
            value="",
            options=[ft.dropdown.Option("", "Any month")],
            **_dd,
        )
        self._filter_tags: list[str] = []
        self._filter_colls: list[str] = []
        self._tags_mode = "and"  # and | or
        self._tag_chips = ft.Row(spacing=4, tight=True, wrap=True, run_spacing=4)
        self._coll_chips = ft.Row(spacing=4, tight=True, wrap=True, run_spacing=4)
        self._dl_only = ft.Checkbox(label="Downloadable", value=False)
        self._disk_only = ft.Checkbox(
            label="On drive",
            value=False,
            tooltip="Only models with a GLB on disk",
        )
        self._hide_downloaded = ft.Checkbox(
            label="Hide downloaded",
            value=False,
            tooltip="Hide models already saved to disk (Downloaded = yes in workbook)",
        )
        self._orig_only = ft.Checkbox(label="Has original", value=False, tooltip="Author included a source archive (FBX/OBJ/…) on Sketchfab")
        self._assign_dd = ft.Dropdown(
            label="Assign to",
            width=120,
            dense=True,
            text_size=12,
            options=[],
            content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
            tooltip="Target collection for Assign / Remove / Unlisted (not a filter)",
            on_change=lambda e: self._toolbar_assign_dropdown(quiet=True),
        )
        self._assign_type_field = ft.TextField(
            label="New / type",
            hint_text="Type or create…",
            width=200,
            dense=True,
            text_size=12,
            tooltip="Enter assigns typed name to selected cards",
            on_submit=lambda e: self._toolbar_assign_typed(),
            on_focus=lambda e: self._set_text_focus(True),
            on_blur=lambda e: self._set_text_focus(False),
        )
        self._assign_btn = ft.IconButton(
            icon=ft.Icons.FOLDER,
            tooltip="Assign selected → Collection dropdown",
            on_click=lambda e: self._toolbar_assign_dropdown(quiet=False),
        )
        self._remove_btn = ft.IconButton(
            icon=ft.Icons.FOLDER_OFF,
            tooltip="Remove selected from Collection dropdown (Sketchfab + workbook)",
            on_click=lambda e: self._toolbar_remove_from_collection(),
        )
        self._unlisted_btn = ft.IconButton(
            icon=ft.Icons.VISIBILITY_OFF,
            tooltip="Mark selected as Unlisted (private / can't collect — keeps like; uses Collection dropdown for N/W labels)",
            on_click=lambda e: self._toolbar_mark_unlisted(),
        )
        self._assign_type_btn = ft.IconButton(
            icon=ft.Icons.ADD_CIRCLE_OUTLINE,
            tooltip="Assign typed name (creates collection if new)",
            on_click=lambda e: self._toolbar_assign_typed(),
        )
        self._result_label = ft.Text(
            "",
            size=11,
            color=ft.Colors.GREY_400,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
            expand=True,
        )
        self._auto_btn = ft.IconButton(
            icon=ft.Icons.PLAY_ARROW,
            tooltip="Autoscroll (Space)",
            on_click=lambda e: self.toggle_autoscroll(),
        )
        self._auto_speed_slider = ft.Slider(
            min=120,
            max=2400,
            divisions=38,
            value=480,
            width=90,
            height=28,
            label="{value} px/s",
            tooltip="Autoscroll speed",
            on_change=lambda e: self._on_auto_speed(e),
        )
        self._auto_row = ft.Row(
            [
                self._auto_btn,
                ft.Text("Auto", size=11, color=ft.Colors.GREY_500),
                self._auto_speed_slider,
            ],
            spacing=2,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self._filter_panel = ft.Container(
            visible=False,
            width=float("inf"),
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            bgcolor=_PANEL,
            border_radius=8,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._author_dd,
                            self._category_dd,
                            self._license_dd,
                            self._liked_sort_dd,
                            self._liked_when_dd,
                            self._liked_month_dd,
                            self._dl_only,
                            self._orig_only,
                        ],
                        spacing=8,
                        wrap=False,
                        scroll=ft.ScrollMode.AUTO,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            self._status_dd,
                            self._tag_dd,
                            self._tag_mode_seg,
                            self._tag_chips,
                            self._coll_filter_dd,
                            self._coll_chips,
                            ft.ElevatedButton(
                                "Apply",
                                icon=ft.Icons.SEARCH,
                                height=36,
                                on_click=lambda e: self._apply_filters(immediate=True),
                            ),
                            ft.OutlinedButton("Clear", height=36, on_click=lambda e: self._clear_filters()),
                            ft.OutlinedButton(
                                "Assign filtered",
                                icon=ft.Icons.FOLDER_SHARED,
                                height=36,
                                tooltip="Assign chosen Collection (toolbar) to every filtered model",
                                on_click=lambda e: self._bulk_assign(),
                            ),
                        ],
                        spacing=8,
                        wrap=False,
                        scroll=ft.ScrollMode.AUTO,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
        )

        self._view_seg = ft.SegmentedButton(
            selected={"grid"},
            segments=[
                ft.Segment(value="grid", label=ft.Text("Grid"), icon=ft.Icon(ft.Icons.GRID_VIEW)),
                ft.Segment(value="list", label=ft.Text("List"), icon=ft.Icon(ft.Icons.VIEW_LIST)),
            ],
            on_change=self._on_view_change,
        )
        self._cols_slider = ft.Slider(
            min=float(_MIN_COLS),
            max=float(_MAX_COLS),
            divisions=_MAX_COLS - _MIN_COLS,
            value=float(_DEFAULT_COLS),
            width=100,
            height=28,
            label="{value}/row",
            on_change=self._on_cols_change,
        )
        self._cols_row = ft.Row(
            [
                ft.Text("Cols", size=11, color=ft.Colors.GREY_500),
                self._cols_slider,
            ],
            spacing=4,
            visible=True,
        )

        self.pager = ft.Row(spacing=0, tight=True)
        self.header_bar = ft.Container(visible=False)
        self.scroller = ft.ListView(expand=True, spacing=4, padding=4, on_scroll_interval=40)
        self.scroller.controls = [
            ft.Container(
                padding=24,
                content=ft.Text("Loading likes…", size=13, color=ft.Colors.GREY_500),
            )
        ]
        self._scroller_wrap = wrap_middle_drag_scroll(
            self.scroller,
            expand=True,
            on_scroll=self._on_scroller_scroll,
            on_offset=self._on_autoscroll_offset,
        )
        self._lod_debouncer = ScrollLodDebouncer(self._lod, self, wait_s=0.02, throttle_s=0.012)
        self._result_label.value = "Loading…"

        self._toolbar_row = ft.Container(
            padding=ft.padding.only(top=6),
            content=ft.Row(
                [
                    self._query,
                    ft.IconButton(
                        icon=ft.Icons.FILTER_LIST,
                        tooltip="Filters",
                        on_click=lambda e: self._toggle_filters(),
                    ),
                    ft.Container(
                        expand=True,
                        content=ft.Row(
                            [
                                self._assign_dd,
                                self._assign_btn,
                                self._remove_btn,
                                self._unlisted_btn,
                                self._assign_type_field,
                                self._assign_type_btn,
                                self._page_select,
                                self._sel_label,
                                self._clear_sel_btn,
                                self._series_btn,
                                self._disk_only,
                                self._hide_downloaded,
                                self.pager,
                                self._cols_row,
                                self._view_seg,
                                self._result_label,
                                self._auto_row,
                            ],
                            spacing=4,
                            wrap=False,
                            scroll=ft.ScrollMode.AUTO,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                ],
                spacing=4,
                wrap=False,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        # Chrome-only update target — toggling filters must NOT remount the card grid.
        self._chrome = ft.Column(
            [self._toolbar_row, self._filter_panel],
            spacing=2,
            tight=True,
        )
        self.controls = [
            self._chrome,
            self.header_bar,
            self._scroller_wrap,
        ]

        for ctrl in (
            self._author_dd,
            self._category_dd,
            self._license_dd,
            self._status_dd,
            self._liked_sort_dd,
            self._liked_when_dd,
            self._liked_month_dd,
        ):
            ctrl.on_change = lambda e: self._apply_filters()
        self._dl_only.on_change = lambda e: self._apply_filters()
        self._disk_only.on_change = self._on_disk_only_change
        self._hide_downloaded.on_change = self._on_hide_downloaded_change
        self._orig_only.on_change = lambda e: self._apply_filters()
        if page is not None:
            self.set_page(page)

    def set_page(self, page: ft.Page) -> None:
        self._page = page
        try:
            page.on_keyboard_event = self._on_keyboard
        except Exception:
            pass

    def _set_text_focus(self, focused: bool) -> None:
        self._text_focus = bool(focused)

    def _on_keyboard(self, e: ft.KeyboardEvent) -> None:
        key = (getattr(e, "key", None) or "").strip()
        if key not in {" ", "Space", "Spacebar"}:
            return
        if self._text_focus:
            return
        # Don't insert a space into focused fields — Space toggles autoscroll.
        self.toggle_autoscroll()

    def _on_auto_speed(self, e) -> None:
        try:
            self._auto_speed = float(e.control.value or 480)
        except (TypeError, ValueError):
            self._auto_speed = 480.0

    def toggle_autoscroll(self, *, force: bool | None = None) -> None:
        self._auto_scroll = (not self._auto_scroll) if force is None else bool(force)
        self._auto_btn.icon = ft.Icons.PAUSE if self._auto_scroll else ft.Icons.PLAY_ARROW
        self._auto_btn.icon_color = ft.Colors.LIGHT_BLUE_300 if self._auto_scroll else None
        self._auto_btn.tooltip = "Stop autoscroll (Space)" if self._auto_scroll else "Autoscroll (Space)"
        try:
            self._auto_btn.update()
        except Exception:
            pass
        if self._auto_scroll and self._page and not self._auto_task_running:
            try:
                self._page.run_task(self._autoscroll_loop)
            except Exception:
                self._auto_scroll = False

    async def _autoscroll_loop(self) -> None:
        import asyncio

        self._auto_task_running = True
        # Relative delta avoids yanking back when the user scrolls ahead of the planner.
        interval = 0.028
        try:
            idle_ticks = 0
            while self._auto_scroll and self._page:
                step = max(4.0, float(self._auto_speed) * interval)
                prev = float(self._scroll_offset or 0)
                n_f = len(self._filtered) if self._filtered is not None else 0
                if n_f and self._display_limit < n_f:
                    extent = float(self._max_scroll_extent or 0)
                    if extent <= 0 or (prev + self._viewport_h) >= (extent - _SCROLL_LOAD_THRESHOLD):
                        try:
                            self._load_more_visible()
                        except Exception:
                            pass
                try:
                    self.scroller.scroll_to(
                        delta=step,
                        duration=max(1, int(interval * 1000)),
                        curve=ft.AnimationCurve.LINEAR,
                    )
                except Exception:
                    break
                # Prefetch LOD / load-more from whatever OnScrollEvent reports —
                # do not invent an absolute offset (that fights manual scroll).
                await asyncio.sleep(interval)
                cur = float(self._scroll_offset or 0)
                extent = float(self._max_scroll_extent or 0)
                at_end = (
                    n_f > 0
                    and self._display_limit >= n_f
                    and extent > 0
                    and cur + 32 >= extent
                )
                if at_end or (n_f > 0 and self._display_limit >= n_f and cur <= prev + 0.5):
                    idle_ticks += 1
                    if idle_ticks >= 10:
                        self._auto_scroll = False
                        break
                else:
                    idle_ticks = 0
        finally:
            self._auto_task_running = False
            if not self._auto_scroll:
                self._auto_btn.icon = ft.Icons.PLAY_ARROW
                self._auto_btn.icon_color = None
                self._auto_btn.tooltip = "Autoscroll (Space)"
                try:
                    self._auto_btn.update()
                except Exception:
                    pass

    def set_filter_callback(self, cb) -> None:
        self._on_filter_change = cb

    def set_model_callbacks(self, on_preview=None, on_download=None, on_open_account=None) -> None:
        self._on_preview = on_preview
        self._on_download = on_download
        self._on_open_account = on_open_account

    def set_series_like_callback(self, cb) -> None:
        self._on_series_like = cb

    def get_series_seeds(self) -> tuple[list[str], list[str]]:
        """(names, authors) from selection, else filtered rows (cap 40)."""
        names: list[str] = []
        authors: list[str] = []
        for uid in self.get_selected_uids():
            row = self.selected_row_dict(uid)
            if not row:
                continue
            n = str(row.get("Name") or row.get("Model Name") or "").strip()
            a = str(row.get("Author") or "").strip()
            if n:
                names.append(n)
            if a:
                authors.append(a)
        if names:
            return names, authors
        n_cap = 0
        for _, row in self._filtered.iterrows():
            n = _name(row)
            a = _fmt(row.get("Author"))
            if not n:
                continue
            names.append(n)
            if a:
                authors.append(a)
            n_cap += 1
            if n_cap >= 40:
                break
        return names, authors

    def _click_series_like(self) -> None:
        if not self._on_series_like:
            return
        names, authors = self.get_series_seeds()
        if not names:
            if self._page:
                self._page.open(
                    ft.SnackBar(
                        ft.Text("Select model(s) or filter Liked to a series, then retry Series discover.")
                    )
                )
            return
        self._on_series_like(names, authors)

    def scroll_to_top(self) -> None:
        self._scroll_offset = 0.0
        try:
            self.scroller.scroll_to(offset=0, duration=200)
        except Exception:
            pass

    def set_selection_callback(self, cb) -> None:
        self._on_selection_change = cb

    def get_selected_uids(self) -> list[str]:
        return sorted(self._selected_uids)

    def selected_count(self) -> int:
        return len(self._selected_uids)

    def selected_row_dict(self, uid: str) -> dict | None:
        """Workbook row as plain dict for the selection detail pane."""
        if not uid:
            return None
        for frame in (getattr(self, "_filtered", None), getattr(self, "_source_df", None)):
            if frame is None or getattr(frame, "empty", True):
                continue
            try:
                hit = frame[frame["UID"].astype(str) == str(uid)]
                if hit.empty:
                    continue
                return {k: hit.iloc[0].get(k) for k in hit.columns}
            except Exception:
                continue
        return None

    def clear_selection(self, *, emit: bool = True) -> None:
        self._selected_uids.clear()
        self._sel_anchor = None
        self._page_select.value = False
        # Flip checkboxes in place — do not remount the grid (that reloads every thumb).
        self._paint_selection_checks()
        self._update_sel_label()
        try:
            self._page_select.update()
        except Exception:
            pass
        self.update()
        if emit:
            self._emit_selection()

    def _paint_selection_checks(self) -> None:
        for uid, cb in list(self._sel_cb_by_uid.items()):
            want = uid in self._selected_uids
            if bool(getattr(cb, "value", False)) == want:
                continue
            cb.value = want
            try:
                if getattr(cb, "page", None):
                    cb.update()
            except Exception:
                pass

    def _shift_held(self) -> bool:
        try:
            import sys

            if sys.platform != "win32":
                return False
            import ctypes

            return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)  # VK_SHIFT
        except Exception:
            return False

    def _display_uids(self) -> list[str]:
        return [_uid(row) for _, row in self._page_slice.iterrows() if _uid(row)]

    def _select_range(self, a: str, b: str) -> None:
        order = self._display_uids()
        if a not in order or b not in order:
            self._selected_uids.add(b)
            return
        i, j = order.index(a), order.index(b)
        lo, hi = (i, j) if i <= j else (j, i)
        for u in order[lo : hi + 1]:
            self._selected_uids.add(u)

    def _emit_selection(self) -> None:
        if not self._on_selection_change:
            return
        uids = self.get_selected_uids()
        row = self.selected_row_dict(uids[0]) if len(uids) == 1 else None
        try:
            self._on_selection_change(uids, row)
        except Exception:
            pass

    def _update_sel_label(self) -> None:
        n = len(self._selected_uids)
        self._sel_label.value = f"{n:,} selected" if n else ""
        self._sync_page_select_state()

    def _page_uids(self) -> list[str]:
        return self._display_uids()

    def _sync_page_select_state(self) -> None:
        page_uids = self._page_uids()
        if not page_uids:
            self._page_select.value = False
        else:
            self._page_select.value = all(u in self._selected_uids for u in page_uids)

    def _on_item_check(self, uid: str, checked: bool) -> None:
        if not uid:
            return
        if checked and self._shift_held() and self._sel_anchor and self._sel_anchor != uid:
            self._select_range(self._sel_anchor, uid)
            self._paint_selection_checks()
        else:
            if checked:
                self._selected_uids.add(uid)
                self._sel_anchor = uid
            else:
                self._selected_uids.discard(uid)
                if self._sel_anchor == uid:
                    self._sel_anchor = None
            self._paint_selection_checks()
        self._update_sel_label()
        self.update()
        self._emit_selection()

    def _on_page_select_toggle(self, e) -> None:
        page_uids = self._page_uids()
        if e.control.value:
            self._selected_uids.update(page_uids)
            if page_uids:
                self._sel_anchor = page_uids[0]
        else:
            for u in page_uids:
                self._selected_uids.discard(u)
            self._sel_anchor = None
        self._paint_selection_checks()
        self._update_sel_label()
        self._emit_selection()
        self.update()

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        self._hide_nsfw = hide_nsfw
        self._hide_female = hide_female
        self._hide_male = hide_male

    def _toggle_filters(self) -> None:
        self._filter_panel.visible = not self._filter_panel.visible
        # Only refresh the top chrome — LikedTab.update() re-lays out hundreds of cards (~seconds).
        try:
            self._chrome.update()
        except Exception:
            try:
                self._filter_panel.update()
            except Exception:
                pass

    def _on_view_change(self, e) -> None:
        sel = getattr(e.control, "selected", None) or {"grid"}
        if isinstance(sel, set) and sel:
            self._view_mode = next(iter(sel))
        self._cols_row.visible = self._view_mode == "grid"
        self._render()
        self.update()
        if self._lod_debouncer and self._page:
            self._lod_debouncer.kick(self._page)

    def _on_cols_change(self, e) -> None:
        self._grid_cols = int(round(float(e.control.value or _DEFAULT_COLS)))
        if self._view_mode == "grid" and not self._page_slice.empty:
            self._render()
            self.update()
            if self._lod_debouncer and self._page:
                self._lod_debouncer.kick(self._page)

    def _grid_avail_width(self) -> int:
        if self._grid_avail_w >= 400:
            return int(self._grid_avail_w)
        for ctrl in (self.scroller, getattr(self, "_scroller_wrap", None), self):
            if ctrl is None:
                continue
            try:
                w = float(getattr(ctrl, "width", None) or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w >= 400:
                return int(w)
        page_w = 0
        if self._page is not None:
            try:
                page_w = int(float(self._page.width or 0))
            except (TypeError, ValueError):
                page_w = 0
        if page_w >= 1000:
            return max(520, page_w - _GRID_SIDE_CHROME)
        # Startup / pre-maximize: do not use a 480px floor with 5 columns (tiny cards).
        return _FALLBACK_GRID_AVAIL

    def _effective_cols(self, avail: int | None = None) -> int:
        avail = int(avail if avail is not None else self._grid_avail_width())
        cols = max(_MIN_COLS, min(_MAX_COLS, int(self._grid_cols or _DEFAULT_COLS)))
        while cols > 1 and (avail - _GRID_GAP * (cols - 1)) / max(cols, 1) < _MIN_CARD_W:
            cols -= 1
        return max(1, cols)

    def _card_dims(self) -> tuple[int, int]:
        avail = self._grid_avail_width()
        cols = self._effective_cols(avail)
        inner = max(1, avail - _GRID_GAP * (cols - 1))
        card_w = max(_MIN_CARD_W, int(inner / cols))
        thumb = max(120, card_w - 4)
        self._last_card_w = card_w
        return card_w, thumb

    def on_page_resize(self, width: float | None = None) -> None:
        try:
            w = float(width) if width is not None else float(self._page.width or 0) if self._page else 0.0
        except (TypeError, ValueError):
            w = 0.0
        if w >= 1000:
            self._grid_avail_w = max(520, int(w) - _GRID_SIDE_CHROME)
        self._relayout_if_needed()

    def on_viewport_sync(self) -> None:
        """Lightweight LOD refresh — no grid rebuild (window move / pan)."""
        if self._lod_debouncer and self._page:
            self._lod_debouncer.sync_now(allow_downgrade=False)

    def _mounted(self) -> bool:
        return bool(getattr(self, "page", None))

    def _safe_update(self, *controls: ft.Control | None) -> None:
        if not self._mounted():
            return
        safe_update(*controls, fallback=self)

    def on_tab_shown(self) -> None:
        self._schedule_relayout(delay_s=0.25)

    def _schedule_relayout(self, *, delay_s: float = 0.2) -> None:
        if not self._page:
            return
        self._relayout_gen += 1
        gen = self._relayout_gen

        async def _later():
            import asyncio

            await asyncio.sleep(max(0.0, float(delay_s)))
            if gen != self._relayout_gen:
                return
            try:
                pw = float(self._page.width or 0) if self._page else 0.0
            except (TypeError, ValueError):
                pw = 0.0
            if pw >= 1000:
                self._grid_avail_w = max(520, int(pw) - _GRID_SIDE_CHROME)
            self._relayout_if_needed()
            self._maybe_fill_viewport()

        try:
            self._page.run_task(_later)
        except Exception:
            pass

    def _relayout_if_needed(self) -> None:
        if self._view_mode != "grid" or self._page_slice is None or self._page_slice.empty:
            return
        prev = int(self._last_card_w or 0)
        card_w, _ = self._card_dims()
        if prev and abs(card_w - prev) < 10:
            return
        keep = self._scroll_offset
        self._render()
        self._safe_update(self)
        if keep > 0:
            self._restore_scroll(keep)
        if self._lod_debouncer and self._page:
            self._lod_debouncer.kick(
                self._page, scroll=keep, viewport=self._viewport_h, remount_delay=0.05
            )

    def _maybe_fill_viewport(self) -> None:
        """If cards don't fill the pane yet, load another batch (no scroll needed)."""
        if not self._mounted() or not getattr(self.scroller, "page", None):
            return
        if self._loading_more or self._filtered is None or self._filtered.empty:
            return
        n_f = len(self._filtered)
        if self._display_limit >= n_f:
            return
        extent = float(self._max_scroll_extent or 0)
        # Nothing to scroll → viewport still has room → pull more rows.
        if extent > 80 and (self._scroll_offset + self._viewport_h) < (extent - _SCROLL_LOAD_THRESHOLD):
            return
        self._load_more_visible()
        if self._display_limit < n_f and self._page:
            async def _again():
                import asyncio
                await asyncio.sleep(0.08)
                self._maybe_fill_viewport()

            try:
                self._page.run_task(_again)
            except Exception:
                pass

    def _filter_kwargs(self) -> dict:
        lic = self._license_dd.value or "(all)"
        return {
            "query": self._query.value or "",
            "author": self._author_dd.value or "(all)",
            "category": self._category_dd.value or "(all)",
            "tag": "",
            "tags": list(self._filter_tags),
            "license": lic if lic != "(all)" else "",
            "assignment": self._status_dd.value or "all",
            "downloadable_only": bool(self._dl_only.value),
            "on_disk_only": bool(self._disk_only.value),
            "hide_downloaded": bool(self._hide_downloaded.value),
            "original_only": bool(self._orig_only.value),
            "collection": "",
            "collections": list(self._filter_colls),
            "tags_mode": self._tags_mode,
            "liked_sort": self._liked_sort_dd.value or "order_asc",
            "liked_when": self._liked_when_dd.value or "",
            "liked_month": self._liked_month_dd.value or "",
        }

    def _on_tag_mode(self) -> None:
        sel = getattr(self._tag_mode_seg, "selected", None) or {"and"}
        self._tags_mode = "or" if "or" in sel else "and"
        self._apply_filters()

    def _filter_chip(self, label: str, *, kind: str) -> ft.Control:
        return ft.Container(
            bgcolor="#1e3a5f" if kind == "coll" else "#3f2d0a",
            border=ft.border.all(1, "#38bdf8" if kind == "coll" else "#a16207"),
            border_radius=12,
            padding=ft.padding.only(left=8, right=2, top=1, bottom=1),
            content=ft.Row(
                [
                    ft.Text(label, size=11, color="#e2e8f0", no_wrap=True),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE,
                        icon_size=12,
                        icon_color=ft.Colors.RED_200,
                        tooltip=f"Remove {label}",
                        style=ft.ButtonStyle(padding=0),
                        on_click=lambda e, L=label, k=kind: self._remove_filter_chip(L, k),
                    ),
                ],
                spacing=0,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _paint_filter_chips(self) -> None:
        self._tag_chips.controls = [self._filter_chip(t, kind="tag") for t in self._filter_tags]
        self._coll_chips.controls = [self._filter_chip(c, kind="coll") for c in self._filter_colls]
        try:
            self._tag_chips.update()
            self._coll_chips.update()
        except Exception:
            pass

    def _add_filter_tag(self) -> None:
        val = (self._tag_dd.value or "").strip()
        self._tag_dd.value = "(all)"
        try:
            self._tag_dd.update()
        except Exception:
            pass
        if not val or val == "(all)":
            return
        if val.casefold() in {t.casefold() for t in self._filter_tags}:
            return
        self._filter_tags.append(val)
        self._paint_filter_chips()
        self._apply_filters()

    def _add_filter_coll(self) -> None:
        val = (self._coll_filter_dd.value or "").strip()
        self._coll_filter_dd.value = "(all)"
        try:
            self._coll_filter_dd.update()
        except Exception:
            pass
        if not val or val == "(all)":
            return
        if val.casefold() in {c.casefold() for c in self._filter_colls}:
            return
        self._filter_colls.append(val)
        self._paint_filter_chips()
        self._apply_filters()

    def _remove_filter_chip(self, label: str, kind: str) -> None:
        want = (label or "").casefold()
        if kind == "tag":
            self._filter_tags = [t for t in self._filter_tags if t.casefold() != want]
        else:
            self._filter_colls = [c for c in self._filter_colls if c.casefold() != want]
        self._paint_filter_chips()
        self._apply_filters()

    def _run_filter(self) -> pd.DataFrame:
        df = filter_liked_df(self._source_df, **self._filter_kwargs())
        return filter_dataframe(df, hide_nsfw=self._hide_nsfw, hide_female=self._hide_female, hide_male=self._hide_male)

    def _result_status_text(self, *, shown: int, n_f: int, n_src: int | None = None) -> str:
        if n_src is None:
            n_src = len(self._source_df) if self._source_df is not None else n_f
        bits = [f"{shown:,} loaded · {n_f:,} match"]
        if n_f != n_src:
            bits[0] += f"  (of {n_src:,})"
        if n_f < n_src and (self._hide_nsfw or self._hide_female or self._hide_male):
            hidden = n_src - n_f
            bits.append(f"N/W/M hiding {hidden:,}")
        if self._filter_colls:
            bits.append("coll " + "+".join(self._filter_colls[:3]) + ("…" if len(self._filter_colls) > 3 else ""))
        if self._filter_tags:
            joiner = " & " if self._tags_mode == "and" else " | "
            bits.append(
                f"tag({self._tags_mode}) "
                + joiner.join(self._filter_tags[:3])
                + ("…" if len(self._filter_tags) > 3 else "")
            )
        if shown < n_f:
            bits.append("scroll for more")
        elif n_f > 0 and shown >= n_f and n_f >= n_src:
            bits.append("all on screen")
        return " · ".join(bits)

    def _on_disk_only_change(self, e) -> None:
        if bool(self._disk_only.value):
            self._hide_downloaded.value = False
            try:
                self._hide_downloaded.update()
            except Exception:
                pass
        self._apply_filters()

    def _on_hide_downloaded_change(self, e) -> None:
        if bool(self._hide_downloaded.value):
            self._disk_only.value = False
            try:
                self._disk_only.update()
            except Exception:
                pass
        self._apply_filters()

    def _clear_filters(self) -> None:
        self._query.value = ""
        self._author_dd.value = "(all)"
        self._category_dd.value = "(all)"
        self._tag_dd.value = "(all)"
        self._coll_filter_dd.value = "(all)"
        self._license_dd.value = "(all)"
        self._status_dd.value = "all"
        self._liked_sort_dd.value = "order_asc"
        self._liked_when_dd.value = ""
        self._liked_month_dd.value = ""
        self._dl_only.value = False
        self._disk_only.value = False
        self._hide_downloaded.value = False
        self._orig_only.value = False
        self._filter_tags.clear()
        self._filter_colls.clear()
        self._tags_mode = "and"
        try:
            self._tag_mode_seg.selected = {"and"}
        except Exception:
            pass
        self._paint_filter_chips()
        self._apply_filters(immediate=True)

    def _apply_filters(self, *, immediate: bool = False) -> None:
        if immediate or not self._page:
            self._refilter(reset_scroll=True)
            if self._on_filter_change:
                self._on_filter_change(0)
            return
        self._filter_debounce_gen += 1
        gen = self._filter_debounce_gen
        page = self._page

        async def _tick():
            import asyncio

            await asyncio.sleep(0.25)
            if gen != self._filter_debounce_gen:
                return
            self._refilter(reset_scroll=True)
            if self._on_filter_change:
                self._on_filter_change(0)

        try:
            page.run_task(_tick)
        except Exception:
            self._refilter(reset_scroll=True)
            if self._on_filter_change:
                self._on_filter_change(0)

    def _bulk_assign(self) -> None:
        coll = self._assign_dd.value
        if coll and self._on_bulk_assign:
            uids = [_uid(row) for _, row in self._filtered.iterrows() if _uid(row)]
            if uids:
                self._on_bulk_assign(uids, coll)

    def _selected_or_warn(self) -> list[str]:
        uids = self.get_selected_uids()
        if uids:
            return uids
        if self._page:
            self._page.open(ft.SnackBar(ft.Text("Select models first (☑), or use Assign filtered in Filters.")))
        return []

    def _toolbar_assign_dropdown(self, *, quiet: bool = False) -> None:
        if self._suppress_assign_change:
            return
        coll = (self._assign_dd.value or "").strip()
        if not coll:
            if not quiet and self._page:
                self._page.open(ft.SnackBar(ft.Text("Pick a Collection first.")))
            return
        uids = self.get_selected_uids()
        if not uids:
            if not quiet and self._page:
                self._page.open(ft.SnackBar(ft.Text("Select models first (☑), or use Assign filtered in Filters.")))
            return
        if self._on_bulk_assign:
            self._on_bulk_assign(uids, coll)

    def _toolbar_assign_typed(self) -> None:
        raw = (self._assign_type_field.value or "").strip()
        if not raw:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("Type a collection name first.")))
            return
        uids = self._selected_or_warn()
        if not uids:
            return
        coll = self._resolve_collection_name(raw)
        self._assign_type_field.value = ""
        try:
            self._assign_type_field.update()
        except Exception:
            pass
        if coll:
            if self._on_bulk_assign:
                self._on_bulk_assign(uids, coll)
            return
        if self._on_assign_create:
            # Create / assign every selected model (not just the first).
            self._on_assign_create(uids, raw)
            return
        if self._page:
            self._page.open(ft.SnackBar(ft.Text(f"No collection matching '{raw}'.")))

    def _assign_from_dropdown(self, uid: str, dropdown: ft.Dropdown) -> None:
        coll = (dropdown.value or "").strip()
        if not coll:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("Pick a collection from the dropdown first.")))
            return
        if uid and self._on_assign:
            self._on_assign(uid, coll)

    def _all_collection_names(self) -> list[str]:
        """Full collection list for resolve/chips (not the dropdown's 120 cap)."""
        return list_collection_names(self._colls_df, limit=50_000)

    def _collection_name_map(self) -> dict[str, str]:
        return {n.casefold(): n for n in self._all_collection_names() if n}

    def _resolve_collection_name(self, typed: str) -> str | None:
        want = (typed or "").strip()
        if not want:
            return None
        return match_collection_name(want, self._all_collection_names())

    def _assign_typed(self, uid: str, field: ft.TextField) -> None:
        raw = (field.value or "").strip()
        if not raw:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("Type a collection name first.")))
            return
        coll = self._resolve_collection_name(raw)
        if coll:
            field.value = ""
            try:
                field.update()
            except Exception:
                pass
            if uid and self._on_assign:
                self._on_assign(uid, coll)
            return
        # No existing collection — create on Sketchfab then assign (if callback wired).
        if uid and self._on_assign_create:
            field.value = ""
            try:
                field.update()
            except Exception:
                pass
            self._on_assign_create(uid, raw)
            return
        if self._page:
            self._page.open(
                ft.SnackBar(
                    ft.Text(
                        f"No collection matching '{raw}' — create it via typed assign "
                        "(restart if Create isn't wired) or pick from the dropdown."
                    )
                )
            )

    def _toolbar_mark_unlisted(self) -> None:
        uids = list(self._selected_uids)
        if not uids:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("Select one or more cards first.")))
            return
        extras = []
        coll = (self._assign_dd.value or "").strip()
        if coll:
            extras.append(coll)
        if self._on_mark_unlisted:
            self._on_mark_unlisted(uids, extras)

    def _toolbar_remove_from_collection(self) -> None:
        coll = (self._assign_dd.value or "").strip()
        if not coll:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("Pick a Collection, then Remove.")))
            return
        uids = list(self._selected_uids)
        if not uids:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("Select one or more cards first.")))
            return
        if self._on_remove_from_collection:
            self._on_remove_from_collection(uids, coll)

    def set_assign_callbacks(
        self,
        on_assign,
        on_bulk_assign,
        on_dismiss=None,
        on_assign_create=None,
        on_remove_from_collection=None,
        on_mark_unlisted=None,
    ) -> None:
        self._on_assign = on_assign
        self._on_bulk_assign = on_bulk_assign
        self._on_dismiss = on_dismiss
        self._on_assign_create = on_assign_create
        self._on_remove_from_collection = on_remove_from_collection
        self._on_mark_unlisted = on_mark_unlisted

    def _quick_letter_assign(self, uid: str, letter: str) -> None:
        """Card quick buttons → collections via quick_assign.yaml."""
        letter = (letter or "").upper()
        # Card F = Foods (yaml FOODS); toolbar F = Female stays separate.
        if letter not in ("N", "W", "M", "S", "E", "P", "F") or not uid:
            return
        coll = self._qa_targets.get(letter) or ""
        if not coll:
            key = {"N": "N", "W": "F", "M": "M", "S": "S", "E": "E", "P": "P", "F": "FOODS"}[letter]
            coll = resolve_collection(key, self._all_collection_names())
            if coll:
                self._qa_targets[letter] = coll
        if not coll:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text(f"No collection for {letter} — edit data/quick_assign.yaml")))
            return
        if self._on_assign:
            self._on_assign(uid, coll)

    def _nwm_buttons(self, uid: str, *, overlay: bool = False, vertical: bool = False) -> ft.Control:
        def _btn(letter: str, tip: str, color: str) -> ft.Control:
            return ft.Container(
                content=ft.Text(letter, size=9 if vertical else (10 if overlay else 11), weight=ft.FontWeight.BOLD, color=color),
                bgcolor=ft.Colors.with_opacity(0.9, "#000000") if overlay else "#1e293b",
                border=ft.border.all(1, ft.Colors.with_opacity(0.8, "#f8fafc") if overlay else "#334155"),
                border_radius=4,
                width=26 if vertical else None,
                padding=ft.padding.symmetric(horizontal=3 if vertical else (5 if overlay else 6), vertical=1 if vertical else 2),
                height=18 if vertical else (22 if overlay else 24),
                alignment=ft.alignment.center,
                tooltip=tip,
                ink=True,
                on_click=lambda e, u=uid, L=letter: self._quick_letter_assign(u, L),
            )

        btns = [
            _btn("N", "Assign → N Collection", ft.Colors.RED_200),
            _btn("W", "Assign → Female", ft.Colors.GREEN_200),
            _btn("M", "Assign → Male", ft.Colors.CYAN_200),
            _btn("S", "Assign → Scenes", ft.Colors.AMBER_200),
            _btn("E", "Assign → Enemies", ft.Colors.ORANGE_200),
            _btn("P", "Assign → Props", ft.Colors.PURPLE_200),
            _btn("F", "Assign → Foods", ft.Colors.LIME_200),
        ]
        if vertical:
            return ft.Column(btns, spacing=2, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        row = ft.Row(btns, spacing=2, tight=True, wrap=True, run_spacing=2)
        if overlay:
            return ft.Container(
                content=row,
                bgcolor=ft.Colors.with_opacity(0.92, "#020617"),
                border=ft.border.all(1, ft.Colors.with_opacity(0.7, "#94a3b8")),
                border_radius=6,
                padding=4,
            )
        return row

    def _tiny_icon_btn(
        self,
        *,
        icon,
        tooltip: str,
        on_click,
        disabled: bool = False,
        color=None,
    ) -> ft.Control:
        return ft.Container(
            width=22,
            height=22,
            content=ft.IconButton(
                icon=icon,
                icon_size=14,
                tooltip=tooltip,
                disabled=disabled,
                icon_color=color,
                style=ft.ButtonStyle(padding=0),
                on_click=on_click,
            ),
        )

    def _chip_names_for_row(self, row: pd.Series) -> list[str]:
        uid = _uid(row)
        consumed = self._consumed_suggestions.get(uid, set())
        assigned = {
            p.strip().casefold()
            for p in _fmt(row.get("Assigned Collection(s)")).split(",")
            if p.strip()
        }
        coll_map = self._collection_name_map()
        # Group by casefold; prefer real collection spelling over tag-case noise.
        groups: dict[str, list[str]] = {}
        order_keys: list[str] = []
        for col in ("Suggested Collection(s)", "Auto-Assigned Collection(s)", "Fuzzy Match Collection(s)"):
            val = _fmt(row.get(col))
            for part in val.split(","):
                p = part.split(":")[0].strip() if ":" in part else part.strip()
                if not p:
                    continue
                key = p.casefold()
                if key in consumed or key in assigned:
                    continue
                if key not in groups:
                    groups[key] = []
                    order_keys.append(key)
                if p not in groups[key]:
                    groups[key].append(p)

        ordered: list[str] = []
        for key in order_keys:
            variants = groups[key]
            if key in coll_map:
                pick = coll_map[key]
            else:
                # Singular/plural twin (Enemies → Enemy) before falling back to tag casing.
                matched = match_collection_name(variants[0], list(coll_map.values()))
                if matched:
                    pick = matched
                else:
                    titled = [v for v in variants if any(ch.isupper() for ch in v)]
                    pick = titled[0] if titled else variants[0]
            if pick not in ordered:
                ordered.append(pick)
        return ordered[:_CHIP_CAP]

    def _suggestion_chip(self, uid: str, name: str, *, compact: bool = False, chip_h: int | None = None) -> ft.Control:
        # Full collection name — horizontal scroll on the chip row instead of ellipsis wrap.
        h = int(chip_h or (_CHIP_H_DENSE if compact else _CHIP_H))
        chip_w = max(48, min(140 if compact else 160, 12 + len(name) * (6 if compact else 7)))
        label = ft.Text(
            name,
            size=8 if compact else 9,
            color=ft.Colors.BLUE_100,
            no_wrap=True,
            text_align=ft.TextAlign.CENTER,
            overflow=ft.TextOverflow.VISIBLE,
        )
        label_box = ft.Container(
            content=label,
            alignment=ft.alignment.center,
            width=chip_w,
            height=h,
            opacity=1.0,
            padding=ft.padding.symmetric(horizontal=4 if compact else 6),
        )
        plus_icon = ft.Icon(ft.Icons.ADD, size=12 if compact else 14, color=ft.Colors.WHITE)
        plus_box = ft.Container(
            content=plus_icon,
            alignment=ft.alignment.center,
            width=chip_w,
            height=h,
            visible=False,
            bgcolor=ft.Colors.with_opacity(0.4, "#000000"),
            border_radius=20,
        )
        stack = ft.Stack([label_box, plus_box], width=chip_w, height=h)
        chip = ft.Container(
            content=stack,
            width=chip_w,
            height=h,
            bgcolor=_CHIP_BG,
            border_radius=20,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            tooltip=f"Assign to {name}",
            ink=True,
            data=f"chip:{uid}:{name}",
            on_click=lambda e, u=uid, n=name: self._on_assign(u, n) if self._on_assign else None,
        )
        self._chip_by_key[(uid, name.casefold())] = chip

        def _hover(e):
            show = str(getattr(e, "data", "")) == "true"
            label_box.opacity = 0.22 if show else 1.0
            plus_box.visible = show
            chip.bgcolor = _CHIP_HOVER if show else _CHIP_BG
            try:
                chip.update()
            except Exception:
                pass

        chip.on_hover = _hover
        return chip

    def _remove_from_already(self, uid: str, collection: str) -> None:
        uid = str(uid or "").strip()
        coll = str(collection or "").strip()
        if not uid or not coll:
            return
        if coll.casefold() == "unlisted":
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("Unlisted is a local shelf label — use Mark Unlisted tools to change it.")))
            return
        if self._on_remove_from_collection:
            self._on_remove_from_collection([uid], coll)

    def _already_in_chip(self, uid: str, coll: str, *, compact: bool = True) -> ft.Control:
        short = coll if len(coll) <= (10 if compact else 16) else (coll[:8] + "…" if compact else coll[:14] + "…")
        is_unlisted = coll.casefold() == "unlisted"
        kids: list[ft.Control] = [
            ft.Text(
                short,
                size=8 if compact else 9,
                weight=ft.FontWeight.W_600,
                color="#ecfdf5",
                tooltip=coll,
                no_wrap=True,
            ),
        ]
        if not is_unlisted:
            kids.append(
                ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_size=11,
                    icon_color="#fecaca",
                    tooltip=f"Remove from {coll} on Sketchfab",
                    style=ft.ButtonStyle(padding=0),
                    on_click=lambda e, u=uid, c=coll: self._remove_from_already(u, c),
                )
            )
        return ft.Container(
            bgcolor=ft.Colors.with_opacity(0.92, "#064e3b"),
            border=ft.border.all(1, _STATUS_IN),
            border_radius=10,
            padding=ft.padding.only(left=6, right=1 if not is_unlisted else 6, top=0, bottom=0),
            content=ft.Row(
                kids,
                spacing=0,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _already_in_chips_row(
        self,
        uid: str,
        names: list[str],
        *,
        compact: bool = True,
        max_width: int | None = None,
        wrap: bool = False,
    ) -> ft.Control:
        show = [n for n in names if n]
        if not show:
            return ft.Container(height=0, width=0, visible=False)
        chips = [self._already_in_chip(uid, n, compact=compact) for n in show]
        row = ft.Row(
            chips,
            spacing=2 if compact else 3,
            wrap=wrap,
            run_spacing=2,
            tight=True,
            scroll=None if wrap else ft.ScrollMode.AUTO,
        )
        if uid:
            self._already_chips_by_uid[uid] = row
        return ft.Container(
            content=row,
            width=max_width,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

    def _rebuild_already_chips(self, uid: str, names: list[str]) -> None:
        row = self._already_chips_by_uid.get(uid)
        if row is None:
            return
        show = [n for n in names if n]
        row.controls = [self._already_in_chip(uid, n, compact=True) for n in show]
        try:
            parent = getattr(row, "parent", None)
            if parent is not None and hasattr(parent, "visible"):
                parent.visible = bool(show)
            if getattr(row, "page", None):
                row.update()
                if parent is not None and getattr(parent, "page", None):
                    parent.update()
        except Exception:
            pass

    def _suggestion_chips(
        self,
        row: pd.Series,
        *,
        compact: bool = False,
        max_width: int | None = None,
        overlay: bool = False,
        chip_cap: int | None = None,
        chip_h: int | None = None,
    ) -> ft.Control:
        uid = _uid(row)
        names = self._chip_names_for_row(row)
        if chip_cap is not None:
            names = names[: max(0, int(chip_cap))]
        # Fixed-height zone (non-overlay) so chips never push assign controls out.
        if not names:
            if overlay:
                return ft.Container(height=0, width=0, visible=False)
            return ft.Container(height=_CHIP_ZONE_H if compact else 0, width=max_width)
        chips = [
            self._suggestion_chip(uid, n, compact=compact, chip_h=chip_h) for n in names
        ]
        row_ctrl = ft.Row(
            chips,
            spacing=2 if compact else 4,
            wrap=False,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
        )
        return ft.Container(
            content=row_ctrl,
            width=max_width,
            height=None if overlay else (_CHIP_ZONE_H if compact else None),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

    def consume_suggestion_chip(self, uid: str, collection: str) -> None:
        """Hide a chip after accept — keep layout slot (opacity) to avoid ListView reflow."""
        uid = str(uid or "").strip()
        coll = str(collection or "").strip()
        if not uid or not coll:
            return
        self._consumed_suggestions.setdefault(uid, set()).add(coll.casefold())
        chip = self._chip_by_key.pop((uid, coll.casefold()), None)
        if chip is not None:
            chip.opacity = 0.0
            chip.disabled = True
            chip.ink = False
            # No chip.update() — painted with the card in one shot.
        label = self._assigned_label_by_uid.get(uid)
        if label is not None:
            label.value = f"→ {coll}"
            label.color = ft.Colors.AMBER_200
            label.visible = True

    def _paint_card_status(self, uid: str, collection: str, *, dim: bool = False) -> None:
        """In-place border/badge/tint — no scroller rebuild, thumbs stay mounted."""
        ctrl = self._card_by_uid.get(uid)
        if ctrl is None:
            return
        badge = self._status_badge_by_uid.get(uid)
        already = bool(badge is not None and getattr(badge, "data", None) in ("in", "pending_in"))
        try:
            ctrl.border = _status_border(True, already)
            if self._view_mode == "grid":
                ctrl.bgcolor = _status_bg(True, already, grid=True)
            else:
                ctrl.bgcolor = _BG_PENDING
            if dim:
                ctrl.opacity = 0.22
            elif uid in self._hidden_uids:
                ctrl.opacity = 0.22
            else:
                ctrl.opacity = 1.0
            tint = _status_tint60(True, already)
            for rail in self._status_rail_by_uid.get(uid, []):
                rail.bgcolor = tint
            if badge is not None:
                short = collection if len(collection) <= 14 else (collection[:12] + "…")
                txt = badge.content
                if isinstance(txt, ft.Text):
                    txt.value = f"→ {short}"
                    txt.color = "#111827"
                badge.bgcolor = _STATUS_PENDING
                badge.data = "pending_in" if already else "pending"
                badge.visible = True
            # Do NOT scroll_to here — restoring a stale offset yanks the list backward.
            if getattr(ctrl, "page", None):
                ctrl.update()
        except Exception:
            pass

    def paint_downloaded(self, uid: str, *, on_disk: bool = True) -> None:
        """Flip the on-disk icon only — keep thumbs mounted (no grid rebuild)."""
        uid = str(uid or "").strip()
        if not uid:
            return
        btn = self._disk_icon_by_uid.get(uid)
        if btn is None:
            return
        try:
            target = btn
            if isinstance(btn, ft.Container) and isinstance(getattr(btn, "content", None), ft.IconButton):
                target = btn.content
            if isinstance(target, ft.IconButton):
                target.icon_color = ft.Colors.GREEN_300 if on_disk else "#f1f5f9"
                target.disabled = not on_disk
                target.tooltip = "Open in Explorer" if on_disk else "Not on disk yet"
            elif isinstance(target, ft.Icon):
                target.color = ft.Colors.GREEN_400 if on_disk else ft.Colors.GREY_700
            if getattr(btn, "page", None):
                btn.update()
            elif getattr(target, "page", None):
                target.update()
        except Exception:
            pass

    def _assign_one(self, model_uid: str) -> None:
        coll = self._assign_dd.value
        if not coll:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("Pick a Collection in the toolbar, then Assign.")))
            return
        if model_uid and self._on_assign:
            self._on_assign(model_uid, coll)

    def _preview_one(self, uid: str, name: str, thumb_url: str = "", *, rect: dict | None = None) -> None:
        if uid and self._on_preview:
            r = rect or self._rect_by_uid.get(uid)
            try:
                self._on_preview(uid, name, "", thumb_url, rect=r)
            except TypeError:
                self._on_preview(uid, name, "", thumb_url)

    def _download_one(self, uid: str, name: str) -> None:
        if uid and self._on_download:
            self._on_download(uid, name)

    def _reveal_download(self, uid: str) -> None:
        """Open Explorer at the downloaded file (or its folder)."""
        uid = str(uid or "").strip()
        if not uid:
            return
        path = ""
        df = self._source_df
        if df is not None and not df.empty and "Download Path" in df.columns:
            uc = "UID" if "UID" in df.columns else ("Model UID" if "Model UID" in df.columns else "")
            if uc:
                hit = df[df[uc].astype(str) == uid]
                if not hit.empty:
                    path = str(hit.iloc[0].get("Download Path") or "").strip()
        if not path or path.casefold() in {"nan", "none", "<na>"}:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text("No download path on file — run Scan DL / Organize.")))
            return
        try:
            from pathlib import Path
            import os
            import subprocess
            import sys

            p = Path(path)
            if not p.exists():
                if self._page:
                    self._page.open(ft.SnackBar(ft.Text(f"Missing on disk: {p.name}")))
                return
            # Normalize — explorer chokes on \\?\ and bad /select, quoting with spaces.
            target = os.path.normpath(str(p.resolve(strict=False)))
            if sys.platform == "win32":
                if os.path.isfile(target):
                    # Must be one shell string with quotes — list argv form opens Documents on spaces.
                    subprocess.Popen(f'explorer /select,"{target}"', shell=True)
                else:
                    subprocess.Popen(f'explorer "{target}"', shell=True)
            elif sys.platform == "darwin":
                if os.path.isfile(target):
                    subprocess.Popen(["open", "-R", target])
                else:
                    subprocess.Popen(["open", target])
            else:
                folder = target if os.path.isdir(target) else os.path.dirname(target)
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            if self._page:
                self._page.open(ft.SnackBar(ft.Text(f"Could not open folder: {exc}")))

    def _refresh_dropdowns(self) -> None:
        self._suppress_assign_change = True
        try:
            self._author_dd.options = [ft.dropdown.Option("(all)")] + [ft.dropdown.Option(a) for a in list_authors(self._source_df)]
            self._category_dd.options = [ft.dropdown.Option("(all)")] + [ft.dropdown.Option(c) for c in list_categories(self._source_df)]
            self._tag_dd.options = [ft.dropdown.Option("(all)")] + [ft.dropdown.Option(t) for t in list_tags(self._source_df)]
            lic_opts = list_licenses(self._source_df)
            self._license_dd.options = [ft.dropdown.Option("(all)", "Any copyright")] + [
                ft.dropdown.Option(k, label) for k, label in lic_opts
            ]
            if "Liked At" in self._source_df.columns:
                month_opts = month_options_from_series(self._source_df["Liked At"])
                self._liked_month_dd.options = [ft.dropdown.Option(v, t) for v, t in month_opts]
            names = list_collection_names(self._colls_df)
            self._coll_filter_dd.options = [ft.dropdown.Option("(all)")] + [ft.dropdown.Option(c) for c in names]
            self._assign_dd.options = [ft.dropdown.Option(c) for c in names]
            if names and not self._assign_dd.value:
                self._assign_dd.value = names[0]
        finally:
            self._suppress_assign_change = False
        # Resolve against the full collection list so Male isn't lost to the 120-name dropdown cap
        # (and so "male" can't substring-match Female).
        all_names = self._all_collection_names()
        self._qa_targets = {
            "N": resolve_collection("N", all_names),
            "W": resolve_collection("F", all_names),
            "M": resolve_collection("M", all_names),
            "S": resolve_collection("S", all_names),
            "E": resolve_collection("E", all_names),
            "P": resolve_collection("P", all_names),
            "F": resolve_collection("FOODS", all_names),
        }

    def set_df(
        self,
        df: pd.DataFrame,
        page_idx: int = 0,
        page_size: int = 50,
        colls_df: pd.DataFrame | None = None,
        *,
        reset_scroll: bool = True,
        refresh_dropdowns: bool = True,
        seed_pending: bool = True,
    ) -> None:
        self._source_df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        if colls_df is not None:
            self._colls_df = colls_df
        self._page_size = max(12, int(page_size or 50))
        self._hidden_uids.clear()
        self._consumed_suggestions.clear()
        self._chip_by_key.clear()
        self._assigned_label_by_uid.clear()
        self._status_badge_by_uid.clear()
        self._status_rail_by_uid.clear()
        self._already_chips_by_uid.clear()
        self._disk_icon_by_uid.clear()
        # Rebuild pending chrome from workbook Assigned — don't leave a stale wipe that
        # makes cards look unassigned after tab switches / filter remounts.
        self._pending_by_uid.clear()
        if seed_pending:
            self._seed_pending_from_df()
        if refresh_dropdowns:
            self._refresh_dropdowns()
        self._refilter(reset_scroll=reset_scroll, page_idx=page_idx)

    def _refilter(self, *, reset_scroll: bool = True, page_idx: int = 0) -> None:
        """Re-run filters on the in-memory workbook — no Sketchfab API."""
        self._filtered = self._run_filter()
        n_src = len(self._source_df)
        n_f = len(self._filtered)
        shown = min(self._display_limit, n_f) if n_f else 0
        self._result_label.value = self._result_status_text(shown=shown, n_f=n_f, n_src=n_src)

        if self._filtered.empty:
            self._display_limit = self._page_size
            self._page_slice = self._filtered
            if self._source_df.empty:
                msg = "No liked models yet — click Collect."
            else:
                msg = "No matches — widen filters or clear search."
            self.scroller.controls = [
                ft.Container(padding=20, content=ft.Text(msg, size=13, color=ft.Colors.GREY_500))
            ]
            self.header_bar.visible = False
            self.update()
            return

        if reset_scroll:
            self._display_limit = self._page_size
        self._display_limit = max(self._page_size, min(self._display_limit, n_f))
        _ = page_idx
        self._page_slice = self._filtered.iloc[: self._display_limit]
        shown = len(self._page_slice)
        self._result_label.value = self._result_status_text(shown=shown, n_f=n_f, n_src=n_src)

        threading.Thread(
            target=_prefetch,
            args=([thumb_urls_from_row(r)[0] for _, r in self._page_slice.head(24).iterrows()],),
            daemon=True,
        ).start()

        self._load_token += 1
        keep_scroll = 0.0 if reset_scroll else float(self._scroll_offset or 0)
        if reset_scroll:
            self._lod.clear()
        t_paint = _time.perf_counter()
        self._render()
        self._rendered_visible = len(self._page_slice)
        paint_ms = (_time.perf_counter() - t_paint) * 1000.0
        if reset_scroll:
            try:
                self.scroller.scroll_to(offset=0, duration=0)
            except Exception:
                pass
        else:
            self._restore_scroll(keep_scroll)
        self._update_sel_label()
        self.update()
        if self._lod_debouncer and self._page:
            self._lod_debouncer.kick(
                self._page,
                scroll=0.0 if reset_scroll else keep_scroll,
                viewport=self._viewport_h,
                remount_delay=0.08,
            )
        if paint_ms >= 80:
            perf_log.mark(f"Liked filter {shown:,} cards", paint_ms, quiet=True)
        if reset_scroll:
            self._schedule_relayout(delay_s=0.12)

    def bind_source(self, df: pd.DataFrame) -> None:
        """Keep Liked tab pointed at the live workbook DataFrame (no rebuild)."""
        self._source_df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    def _seed_pending_from_df(self) -> None:
        """Fill _pending_by_uid from Assigned ∉ Already In so remounts keep amber chrome."""
        df = self._source_df
        if df is None or df.empty or "Assigned Collection(s)" not in df.columns:
            return
        uc = "UID" if "UID" in df.columns else ("Model UID" if "Model UID" in df.columns else "")
        if not uc:
            return
        for _, row in df.iterrows():
            uid = _fmt(row.get(uc))
            if not uid:
                continue
            is_pending, _, pend_lbl, _, _ = _row_collection_status(row)
            if not is_pending:
                continue
            assigned = _csv_names(row.get("Assigned Collection(s)"))
            already = {a.casefold() for a in _csv_names(row.get("Already In Collection(s)"))}
            pending = [a for a in assigned if a.casefold() not in already]
            if pending:
                self._pending_by_uid[uid] = ", ".join(pending)
            elif pend_lbl:
                self._pending_by_uid[uid] = pend_lbl

    def paint_download_markers_from_df(self) -> int:
        """Update green disk icons from Downloaded column without remounting the grid."""
        df = self._source_df
        if df is None or df.empty:
            return 0
        uc = "UID" if "UID" in df.columns else ("Model UID" if "Model UID" in df.columns else "")
        if not uc or "Downloaded" not in df.columns:
            return 0
        n = 0
        for uid in list(self._disk_icon_by_uid.keys()):
            hit = df[df[uc].astype(str) == uid]
            if hit.empty:
                continue
            on_disk = str(hit.iloc[0].get("Downloaded") or "").lower() in {"yes", "y", "true", "1"}
            self.paint_downloaded(uid, on_disk=on_disk)
            if on_disk:
                n += 1
        return n

    def show_loading(self, msg: str = "Loading likes…") -> None:
        self._result_label.value = msg
        self.scroller.controls = [
            ft.Container(padding=24, content=ft.Text(msg, size=13, color=ft.Colors.GREY_500))
        ]
        self.header_bar.visible = False
        try:
            self.update()
        except Exception:
            pass

    def apply_assign_instant(self, uid: str, collection: str) -> bool:
        """
        Instant feedback: consume chip + amber pending chrome.
        Filter-hidden collections dim in place (keep height/images — no content remount).
        If the workbook already lists this collection under Already In, paint as synced
        (not fake-pending) so Pending Push / Unpushed stay honest.
        """
        uid = str(uid or "").strip()
        collection = str(collection or "").strip()
        if not uid:
            return False

        # Prefer live workbook row when available.
        already_has = False
        df = self._source_df
        if df is not None and not df.empty and collection:
            uc = "UID" if "UID" in df.columns else ("Model UID" if "Model UID" in df.columns else "")
            if uc:
                hit = df[df[uc].astype(str).str.strip() == uid]
                if not hit.empty:
                    row = hit.iloc[0]
                    already_cf = {a.casefold() for a in _csv_names(row.get("Already In Collection(s)"))}
                    already_has = collection.casefold() in already_cf

        if already_has:
            # Still consume the suggestion chip; don't invent a pending queue entry.
            self.consume_suggestion_chip(uid, collection)
            prev = self._pending_by_uid.get(uid, "")
            names = [p.strip() for p in prev.split(",") if p.strip()]
            names = [n for n in names if n.casefold() != collection.casefold()]
            if names:
                self._pending_by_uid[uid] = ", ".join(names)
                disp = names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1}"
                self._paint_card_status(uid, disp, dim=False)
            else:
                self._pending_by_uid.pop(uid, None)
                try:
                    self.apply_already_in_instant(uid)
                except Exception:
                    self._paint_card_status(uid, "", dim=False)
            return False

        # Merge into any existing pending collections so a card can carry several.
        prev = self._pending_by_uid.get(uid, "")
        names = [p.strip() for p in prev.split(",") if p.strip()]
        if collection and collection.casefold() not in {n.casefold() for n in names}:
            names.append(collection)
        merged = ", ".join(names)
        self._pending_by_uid[uid] = merged
        self.consume_suggestion_chip(uid, collection)
        disp = names[0] if names else collection
        if len(names) > 1:
            disp = f"{names[0]} +{len(names) - 1}"
        # Only dim/hide when every pending collection is filtered out.
        should_hide = bool(names) and all(
            collection_hidden_by_filters(
                n,
                hide_nsfw=self._hide_nsfw,
                hide_female=self._hide_female,
                hide_male=self._hide_male,
            )
            for n in names
        )
        self._paint_card_status(uid, disp, dim=should_hide)
        if should_hide:
            self._selected_uids.discard(uid)
            self._hidden_uids.add(uid)
            return True
        self._hidden_uids.discard(uid)
        return False

    def apply_cancel_instant(self, uid: str, collection: str) -> None:
        """Strip one pending collection from card chrome — no grid remount."""
        uid = str(uid or "").strip()
        collection = str(collection or "").strip()
        if not uid:
            return
        prev = self._pending_by_uid.get(uid, "")
        names = [
            p.strip()
            for p in prev.split(",")
            if p.strip() and p.strip().casefold() != collection.casefold()
        ]
        # Prefer live workbook Assigned if pending cache is stale.
        if self._source_df is not None and not self._source_df.empty:
            try:
                uc = "UID" if "UID" in self._source_df.columns else None
                if uc:
                    hit = self._source_df[self._source_df[uc].astype(str) == uid]
                    if not hit.empty:
                        raw = hit.iloc[0].get("Assigned Collection(s)")
                        already = {
                            a.casefold()
                            for a in _csv_names(hit.iloc[0].get("Already In Collection(s)"))
                        }
                        names = [
                            p
                            for p in _csv_names(raw)
                            if p.casefold() not in already
                        ]
            except Exception:
                pass
        if names:
            self._pending_by_uid[uid] = ", ".join(names)
            disp = names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1}"
            should_hide = all(
                collection_hidden_by_filters(
                    n,
                    hide_nsfw=self._hide_nsfw,
                    hide_female=self._hide_female,
                    hide_male=self._hide_male,
                )
                for n in names
            )
            self._paint_card_status(uid, disp, dim=should_hide)
            if should_hide:
                self._selected_uids.discard(uid)
                self._hidden_uids.add(uid)
            return
        self._pending_by_uid.pop(uid, None)
        self._paint_status_from_row(uid)

    def apply_already_in_instant(self, uid: str) -> None:
        """Refresh card status after Already In changed (e.g. remove from collection)."""
        uid = str(uid or "").strip()
        if not uid:
            return
        self._paint_status_from_row(uid)

    def _paint_status_from_row(self, uid: str) -> None:
        ctrl = self._card_by_uid.get(uid)
        if ctrl is None:
            return
        row = None
        if self._source_df is not None and not self._source_df.empty:
            try:
                uc = "UID" if "UID" in self._source_df.columns else None
                if uc:
                    hit = self._source_df[self._source_df[uc].astype(str) == uid]
                    if not hit.empty:
                        row = hit.iloc[0]
            except Exception:
                row = None
        if row is None:
            return
        override = self._pending_by_uid.get(uid)
        is_pending, is_in, pend_lbl, already_lbl, is_unlisted = _row_collection_status(row, override)
        already_names = _csv_names(row.get("Already In Collection(s)"))
        n_in = len(already_names)
        badge = self._status_badge_by_uid.get(uid)
        try:
            ctrl.border = _status_border(is_pending, is_in)
            if self._view_mode == "grid":
                ctrl.bgcolor = _status_bg(is_pending, is_in, grid=True)
            else:
                ctrl.bgcolor = _BG_PENDING if is_pending else (_BG_IN if is_in else _BG_GRID)
            ctrl.opacity = 1.0
            tint = _status_tint60(is_pending, is_in)
            for rail in self._status_rail_by_uid.get(uid, []):
                rail.bgcolor = tint
            self._rebuild_already_chips(uid, already_names)
            if badge is not None:
                if is_pending:
                    short = pend_lbl if len(pend_lbl) <= 14 else (pend_lbl[:12] + "…")
                    badge_text = f"→ {short}"
                    badge_bg = _STATUS_PENDING
                    badge_data = "pending_in" if is_in else "pending"
                elif is_unlisted and n_in <= 1:
                    badge_text = "Unlisted"
                    badge_bg = _STATUS_UNLISTED
                    badge_data = "unlisted"
                else:
                    badge_text = ""
                    badge_bg = _STATUS_IN
                    badge_data = ""
                txt = badge.content
                if isinstance(txt, ft.Text):
                    txt.value = badge_text
                    txt.color = "#111827"
                badge.bgcolor = badge_bg
                badge.data = badge_data
                badge.visible = bool(badge_text)
                badge.tooltip = _already_tooltip(row)
            if getattr(ctrl, "page", None):
                ctrl.update()
        except Exception:
            pass

    def _iter_card_controls(self):
        for c in self.scroller.controls or []:
            if self._view_mode == "grid":
                row = getattr(c, "content", None)
                for k in getattr(row, "controls", None) or []:
                    yield k
            else:
                yield c

    def note_assigned(self, uid: str, collection: str) -> None:
        """Back-compat — prefer apply_assign_instant."""
        self.apply_assign_instant(uid, collection)

    def _restore_scroll(self, offset: float | None = None) -> None:
        off = self._scroll_offset if offset is None else float(offset or 0)
        if off <= 0:
            return
        try:
            self.scroller.scroll_to(offset=off, duration=0)
        except Exception:
            pass

    def _on_autoscroll_offset(self, offset: float) -> None:
        """Middle-drag scroll_to often skips OnScrollEvent — keep LOD in sync."""
        self._scroll_offset = float(offset or 0)
        if self._lod_debouncer and self._page:
            self._lod_debouncer.nudge(self._page, self._scroll_offset, self._viewport_h)

    def _on_scroller_scroll(self, e: ft.OnScrollEvent) -> None:
        try:
            self._scroll_offset = float(e.pixels or 0)
            self._viewport_h = float(e.viewport_dimension or self._viewport_h or 800)
            self._max_scroll_extent = float(getattr(e, "max_scroll_extent", None) or 0)
        except (TypeError, ValueError):
            pass
        if self._lod_debouncer and self._page:
            self._lod_debouncer.on_scroll(e, self._page)
        if self._loading_more or self._filtered.empty:
            return
        if self._display_limit >= len(self._filtered):
            return
        dragging = getattr(self._scroller_wrap, "is_middle_dragging", None)
        if callable(dragging) and dragging():
            return
        try:
            near_bottom = (self._scroll_offset + self._viewport_h) >= (
                float(e.max_scroll_extent or 0) - _SCROLL_LOAD_THRESHOLD
            )
        except (TypeError, ValueError):
            near_bottom = False
        if near_bottom:
            self._load_more_visible()

    def _load_more_visible(self) -> None:
        if self._loading_more or not self._mounted():
            return
        n_f = len(self._filtered)
        if self._display_limit >= n_f:
            return
        self._loading_more = True
        keep_scroll = self._scroll_offset
        old_limit = self._display_limit
        try:
            self._display_limit = min(n_f, self._display_limit + self._page_size)
            self._page_slice = self._filtered.iloc[: self._display_limit]
            shown = len(self._page_slice)
            self._result_label.value = self._result_status_text(shown=shown, n_f=n_f)
            if old_limit <= 0 or self._rendered_visible <= 0 or not self.scroller.controls:
                self._render()
                self._rendered_visible = shown
            else:
                self._append_visible_rows(old_limit, self._display_limit)
                self._rendered_visible = shown
            self._update_sel_label()
            # One full-tab update — partial scroller.update() breaks on appended cards (no __uid yet).
            try:
                self.update()
            except (AssertionError, RuntimeError):
                self._render()
                self._rendered_visible = shown
                self._safe_update(self)
            self._restore_scroll(keep_scroll)
            if self._lod_debouncer and self._page:
                self._lod_debouncer.kick(
                    self._page, scroll=keep_scroll, viewport=self._viewport_h, remount_delay=0.06
                )
        finally:
            self._loading_more = False

    def _append_visible_rows(self, start: int, end: int) -> None:
        """Append newly visible liked rows without rebuilding earlier cards."""
        if start >= end or self._filtered is None or self._filtered.empty:
            return
        chunk = self._filtered.iloc[start:end]
        if chunk.empty:
            return
        base_index = start
        if self._view_mode == "list":
            cols = [c for c in _LIST_COLS if c in ("Name", "Assign") or c in self._page_slice.columns]
            row_h = 96.0
            gap = 4.0
            for j, (_, row) in enumerate(chunk.iterrows()):
                i = base_index + j
                y0 = i * (row_h + gap)
                ctrl = self._list_row(row, cols, i % 2 == 0, lod_y0=y0, lod_y1=y0 + row_h)
                u = _uid(row)
                if u:
                    self._card_by_uid[u] = ctrl
                self.scroller.controls.append(ctrl)
            return

        card_w, thumb_h = self._card_dims()
        cols_n = self._effective_cols()
        card_h = float(thumb_h)
        gap = float(_GRID_GAP)
        for j, (_, row) in enumerate(chunk.iterrows()):
            i = base_index + j
            row_i = i // cols_n
            col_i = i % cols_n
            y0 = row_i * (card_h + gap)
            card = self._grid_card(row, lod_y0=y0, lod_y1=y0 + card_h)
            card.height = card_h
            u = _uid(row)
            if u:
                self._card_by_uid[u] = card
            if col_i == 0:
                self.scroller.controls.append(
                    ft.Container(
                        padding=ft.padding.only(bottom=4),
                        height=card_h + 4,
                        content=ft.Row([card], spacing=_GRID_GAP, wrap=False),
                    )
                )
            else:
                row_container = self.scroller.controls[-1]
                row_content = getattr(row_container, "content", None)
                if isinstance(row_content, ft.Row):
                    row_content.controls.append(card)
                else:
                    self._render()
                    self._rendered_visible = len(self._page_slice)
                    return

    def _assign_options_for_row(self, row: pd.Series) -> list[str]:
        names = [str(o.key) for o in (self._assign_dd.options or []) if o.key]
        ordered: list[str] = []
        for col in ("Suggested Collection(s)", "Auto-Assigned Collection(s)", "Fuzzy Match Collection(s)"):
            val = _fmt(row.get(col))
            for part in val.split(","):
                p = part.split(":")[0].strip() if ":" in part else part.strip()
                if p and p not in ordered:
                    ordered.append(p)
        for n in names:
            if n not in ordered:
                ordered.append(n)
        return ordered[:50]

    def _render(self) -> None:
        dfp = self._page_slice
        self._card_by_uid = {}
        self._disk_icon_by_uid = {}
        self._sel_cb_by_uid = {}
        self._chip_by_key = {}
        self._assigned_label_by_uid = {}
        self._status_badge_by_uid = {}
        self._status_rail_by_uid = {}
        self._lod.clear()
        if dfp.empty:
            return
        if self._view_mode == "list":
            cols = [c for c in _LIST_COLS if c in ("Name", "Assign") or c in dfp.columns]
            self.header_bar = self._header(cols)
            self.header_bar.visible = True
            # Slot: [chrome, header, scroller]
            if len(self.controls) >= 3:
                self.controls[1] = self.header_bar
            row_h = 96.0
            gap = 4.0
            rows = []
            for i, (_, row) in enumerate(dfp.iterrows()):
                y0 = i * (row_h + gap)
                ctrl = self._list_row(row, cols, i % 2 == 0, lod_y0=y0, lod_y1=y0 + row_h)
                u = _uid(row)
                if u:
                    self._card_by_uid[u] = ctrl
                rows.append(ctrl)
            self.scroller.controls = rows
        else:
            self.header_bar.visible = False
            card_w, thumb_h = self._card_dims()
            cols_n = self._effective_cols()
            # Image-only cards — letters / title / chips / license overlay the thumb.
            card_h = float(thumb_h)
            gap = float(_GRID_GAP)
            cards = []
            for i, (_, row) in enumerate(dfp.iterrows()):
                row_i = i // cols_n
                y0 = row_i * (card_h + gap)
                ctrl = self._grid_card(row, lod_y0=y0, lod_y1=y0 + card_h)
                ctrl.height = card_h
                u = _uid(row)
                if u:
                    self._card_by_uid[u] = ctrl
                cards.append(ctrl)
            grid_rows = []
            for i in range(0, len(cards), cols_n):
                grid_rows.append(
                    ft.Container(
                        padding=ft.padding.only(bottom=4),
                        height=card_h + 4,
                        content=ft.Row(cards[i : i + cols_n], spacing=_GRID_GAP, wrap=False),
                    )
                )
            self.scroller.controls = grid_rows

    def _header(self, cols: list[str]) -> ft.Container:
        cells = [
            ft.Container(
                width=_SEL_COL_W,
                content=ft.Text("☑", size=10, weight=ft.FontWeight.BOLD),
                padding=ft.padding.symmetric(horizontal=2, vertical=6),
            )
        ]
        for c in cols:
            label = _HDR_LABELS.get(c, c)
            w = _COL_WIDTHS.get(c, 100)
            cells.append(
                ft.Container(
                    width=w,
                    content=ft.Text(label, weight=ft.FontWeight.BOLD, size=10, no_wrap=True),
                    padding=ft.padding.symmetric(horizontal=4, vertical=6),
                )
            )
        cells.append(ft.Container(width=_ACTIONS_W, content=ft.Text("Actions", weight=ft.FontWeight.BOLD, size=10, no_wrap=True)))
        return ft.Container(
            content=ft.Row(cells, spacing=_COL_SPACING),
            bgcolor=_HDR_BG,
            border=ft.border.only(bottom=ft.BorderSide(1, _HDR_BORDER)),
        )

    def _grid_card(self, row: pd.Series, *, lod_y0: float = 0.0, lod_y1: float = 0.0) -> ft.Container:
        card_w, thumb_h = self._card_dims()
        low, hi = thumb_urls_from_row(row)
        # First ~3 viewports start HD so opening Liked / scrolling in isn't soft.
        prefer_hi = bool(hi) and lod_y0 <= (self._viewport_h or 800.0) * 3.0
        thumb_box, img = make_lod_thumb(low, hi, size=thumb_h, prefer_hi=prefer_hi)
        if img is not None:
            self._lod.add(
                img,
                low,
                hi,
                lod_y0,
                lod_y1 or (lod_y0 + thumb_h),
                level="hi" if prefer_hi else "low",
            )
        name = _name(row)
        url = _url(row)
        uid = _uid(row)
        lic = normalize_license_label(row.get("License"))
        cats = _fmt(row.get("Categories"))
        tris = _fmt(row.get("Face Count"))
        dl = str(row.get("Downloadable", "")).lower() in {"yes", "y", "true", "1"}
        on_disk = str(row.get("Downloaded", "")).lower() in {"yes", "y", "true", "1"}
        sel_cb = ft.Checkbox(
            value=uid in self._selected_uids,
            tooltip="Shift+click to select a range",
            on_change=lambda e, u=uid: self._on_item_check(u, bool(e.control.value)),
        )
        if uid:
            self._sel_cb_by_uid[uid] = sel_cb

        override = self._pending_by_uid.get(uid)
        is_pending, is_in, pend_lbl, already_lbl, is_unlisted = _row_collection_status(row, override)
        tint = _status_tint60(is_pending, is_in)
        already_names = _csv_names(row.get("Already In Collection(s)"))
        n_in = len(already_names)
        tip_in = _already_tooltip(row)
        badge_text = ""
        badge_bg = _STATUS_PENDING
        badge_data = ""
        # Already In collections are shown as removable chips; badge is for pending / Unlisted only.
        if is_pending:
            short = pend_lbl if len(pend_lbl) <= 14 else (pend_lbl[:12] + "…")
            badge_text = f"→ {short}"
            badge_bg = _STATUS_PENDING
            badge_data = "pending_in" if is_in else "pending"
        elif is_unlisted and n_in <= 1:
            badge_text = "Unlisted"
            badge_bg = _STATUS_UNLISTED
            badge_data = "unlisted"
        status_badge = ft.Container(
            content=ft.Text(badge_text, size=8 if card_w < 190 else 9, weight=ft.FontWeight.BOLD, color="#111827", no_wrap=True),
            bgcolor=badge_bg,
            padding=ft.padding.symmetric(horizontal=5, vertical=1),
            border_radius=4,
            visible=bool(badge_text),
            data=badge_data,
            tooltip=tip_in,
        )
        if uid:
            self._status_badge_by_uid[uid] = status_badge

        disk_btn = self._tiny_icon_btn(
            icon=ft.Icons.SAVE_ALT,
            tooltip="Open in Explorer" if on_disk else "Not on disk yet",
            on_click=lambda e, u=uid: self._reveal_download(u),
            disabled=not on_disk,
            color=ft.Colors.GREEN_300 if on_disk else "#f1f5f9",
        )
        if uid:
            self._disk_icon_by_uid[uid] = disk_btn

        dl_color, dl_tip = download_icon_style(dl)
        rail_actions: list[ft.Control] = [
            ft.Container(content=sel_cb, alignment=ft.alignment.center),
            self._tiny_icon_btn(
                icon=ft.Icons.VIEW_IN_AR,
                tooltip="Open 3D viewer",
                on_click=lambda e, u=uid, n=name, t=low: self._preview_one(u, n, t) if u else None,
                color="#67e8f9",
            ),
            self._tiny_icon_btn(
                icon=ft.Icons.DOWNLOAD_FOR_OFFLINE,
                tooltip=dl_tip,
                on_click=lambda e, u=uid, n=name: self._download_one(u, n),
                color=dl_color,
            ),
            disk_btn,
        ]
        if uid:
            rail_actions.append(self._nwm_buttons(uid, overlay=True, vertical=True))

        left_rail = ft.Container(
            left=0,
            top=0,
            bottom=0,
            width=_RAIL_W,
            bgcolor=tint,
            padding=ft.padding.only(top=4, bottom=4, left=2, right=2),
            content=ft.Column(
                rail_actions,
                spacing=2,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                scroll=ft.ScrollMode.AUTO,
            ),
        )

        author = _fmt(row.get("Author"))[:28] or "-"
        author_key = _fmt(row.get("Author Username")) or _fmt(row.get("Author"))
        likes_n = _fmt(row.get("Likes"))
        lic_short = (lic[:22] + "…") if len(lic) > 22 else lic
        meta_parts: list[str] = []
        if likes_n:
            meta_parts.append(f"{likes_n} likes")
        if lic_short or lic:
            meta_parts.append(lic_short or "License ?")
        if tris:
            meta_parts.append(f"{tris} tris")
        meta2 = " · ".join(meta_parts)
        tip_meta = " · ".join(p for p in (author, cats, lic, (f"{likes_n} likes" if likes_n else "")) if p)

        # Denser chrome on narrow cards (high column count) — less empty scrim over the thumb.
        dense = card_w < 190
        mid = card_w < 230
        title_size = 10 if dense else (11 if mid else 12)
        meta_size = 8 if dense else (9 if mid else 10)
        pad_v = 2 if dense else (3 if mid else 5)
        pad_r = 4 if dense else 6
        gap = 1 if dense else 2
        chip_cap = 3 if dense else (4 if mid else _CHIP_CAP)
        chip_h = _CHIP_H_DENSE if dense or mid else _CHIP_H
        # Slightly wider scrim on tiny cards so text isn't clipped; still hug content vertically.
        text_frac = 0.92 if dense else (0.82 if mid else 0.70)
        text_w = max(_RAIL_W + 8, min(card_w - 4, int(card_w * text_frac)))
        inner_w = max(64, text_w - _RAIL_W - 8)

        title_ctrl = (
            ft.TextButton(
                text=name[: max(14, card_w // 7)],
                url=url,
                style=ft.ButtonStyle(
                    padding=0,
                    color=_META_ON_SCRIM,
                    visual_density=ft.VisualDensity.COMPACT,
                ),
                tooltip=tip_meta or None,
            )
            if url
            else ft.Text(
                name[: max(14, card_w // 7)],
                size=title_size,
                weight=ft.FontWeight.W_700,
                color=_META_ON_SCRIM,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
                tooltip=tip_meta or None,
            )
        )
        # Dense: author + license on one row (saves a full empty text line in the scrim).
        if dense:
            author_bit = (
                ft.TextButton(
                    text=author[:18],
                    style=ft.ButtonStyle(
                        padding=0,
                        color=_META_ON_SCRIM,
                        visual_density=ft.VisualDensity.COMPACT,
                    ),
                    tooltip=f"Open @{author_key} in Account" if author_key else tip_meta,
                    on_click=lambda e, k=author_key: self._on_open_account(k) if self._on_open_account and k else None,
                )
                if author_key and self._on_open_account and author != "-"
                else ft.Text(author[:18], size=meta_size, color=_META_ON_SCRIM, no_wrap=True)
            )
            author_ctrl = None
            meta_ctrl = ft.Row(
                [
                    author_bit,
                    ft.Text("·", size=meta_size, color=_LICENSE_ON_SCRIM),
                    ft.Text(
                        meta2,
                        size=meta_size,
                        weight=ft.FontWeight.W_600,
                        color=_LICENSE_ON_SCRIM,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        expand=True,
                        tooltip=tip_meta or None,
                    ),
                ],
                spacing=3,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        else:
            author_ctrl = (
                ft.TextButton(
                    text=author,
                    style=ft.ButtonStyle(
                        padding=0,
                        color=_META_ON_SCRIM,
                        visual_density=ft.VisualDensity.COMPACT,
                    ),
                    tooltip=f"Open @{author_key} in Account" if author_key else tip_meta,
                    on_click=lambda e, k=author_key: self._on_open_account(k) if self._on_open_account and k else None,
                )
                if author_key and self._on_open_account and author != "-"
                else ft.Text(
                    author,
                    size=meta_size,
                    weight=ft.FontWeight.W_700,
                    color=_META_ON_SCRIM,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=tip_meta or None,
                )
            )
            meta_ctrl = ft.Text(
                meta2,
                size=meta_size,
                weight=ft.FontWeight.W_700,
                color=_LICENSE_ON_SCRIM,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
                tooltip=tip_meta or None,
            )

        already_chips = self._already_in_chips_row(
            uid,
            already_names,
            compact=True,
            max_width=inner_w,
            wrap=False,
        )
        chip_names = self._chip_names_for_row(row)[:chip_cap]
        chips = self._suggestion_chips(
            row,
            compact=True,
            max_width=inner_w,
            overlay=True,
            chip_cap=chip_cap,
            chip_h=chip_h,
        )

        # Only mount rows that have content — empty badge/chip slots used to pad the scrim.
        overlay_kids: list[ft.Control] = []
        if badge_text:
            overlay_kids.append(status_badge)
        if already_names:
            overlay_kids.append(already_chips)
        if chip_names:
            overlay_kids.append(chips)
        overlay_kids.append(title_ctrl)
        if author_ctrl is not None:
            overlay_kids.append(author_ctrl)
        overlay_kids.append(meta_ctrl)

        bottom_text = ft.Container(
            left=0,
            bottom=0,
            width=text_w,
            bgcolor=tint,
            padding=ft.padding.only(left=_RAIL_W + 3, right=pad_r, top=pad_v, bottom=pad_v),
            content=ft.Column(
                overlay_kids,
                spacing=gap,
                tight=True,
            ),
        )

        if uid:
            self._status_rail_by_uid[uid] = [left_rail, bottom_text]

        # Rail last so action buttons stay above the bottom text scrim.
        overlay: list[ft.Control] = [bottom_text, left_rail]

        if dl:
            overlay.append(
                ft.Container(
                    right=4,
                    top=4 if not (
                        str(row.get("Original Source") or "").strip().lower() in {"yes", "y", "true", "1"}
                    ) else 26,
                    bgcolor=ft.Colors.with_opacity(0.92, "#166534"),
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=5, vertical=2),
                    tooltip="Downloadable GLB on Sketchfab",
                    content=ft.Text("DL", size=8, weight=ft.FontWeight.BOLD, color="#bbf7d0", no_wrap=True),
                )
            )
        if on_disk:
            overlay.append(
                ft.Container(
                    right=4,
                    bottom=4,
                    bgcolor=ft.Colors.with_opacity(0.92, "#0f766e"),
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=5, vertical=2),
                    tooltip="GLB on disk",
                    content=ft.Text("DISK", size=8, weight=ft.FontWeight.BOLD, color="#ccfbf1", no_wrap=True),
                )
            )

        orig = str(row.get("Original Source") or "").strip().lower() in {"yes", "y", "true", "1"}
        orig_fmt = str(row.get("Original Format") or "").strip()
        if orig:
            label = (orig_fmt or "SRC").upper()[:8]
            tip = f"Author original archive ({orig_fmt or 'unknown'})"
            if row.get("Original Size"):
                tip += f" · {row.get('Original Size')}"
            overlay.append(
                ft.Container(
                    left=_RAIL_W + 4,
                    top=4,
                    bgcolor=ft.Colors.with_opacity(0.92, "#7c3aed"),
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=5, vertical=2),
                    tooltip=tip,
                    content=ft.Text(label, size=8, weight=ft.FontWeight.BOLD, color="#f8fafc", no_wrap=True),
                )
            )

        return ft.Container(
            width=card_w,
            height=thumb_h,
            bgcolor=_status_bg(is_pending, is_in, grid=True),
            border=_status_border(is_pending, is_in),
            border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            data=uid,
            opacity=0.22 if uid in self._hidden_uids else 1.0,
            content=ft.GestureDetector(
                content=ft.Stack([thumb_box, *overlay], height=thumb_h),
                hover_interval=40,
                on_enter=lambda e, u=uid, h=hi, im=img, cw=card_w, ch=thumb_h: self._card_pointer(
                    e, u, h, im, cw, ch, bump_hd=True
                ),
                on_hover=lambda e, u=uid, h=hi, im=img, cw=card_w, ch=thumb_h: self._card_pointer(
                    e, u, h, im, cw, ch, bump_hd=False
                ),
            ),
        )

    def _card_pointer(
        self,
        e,
        uid: str,
        hi: str,
        img,
        card_w: int,
        card_h: int,
        *,
        bump_hd: bool,
    ) -> None:
        """HD thumb bump + remember overlay rect for the 3D button (no auto-open)."""
        if bump_hd and img is not None and hi:
            try:
                img.src = hi
                img.update()
            except Exception:
                pass
        rect = screen_rect_for_card_image(
            self._page, e, card_w, card_h, rail_w=_RAIL_W, meta_h=68
        )
        if uid and rect:
            self._rect_by_uid[uid] = rect

    def _list_row(
        self,
        row: pd.Series,
        cols: list[str],
        even: bool,
        *,
        lod_y0: float = 0.0,
        lod_y1: float = 96.0,
    ) -> ft.Container:
        uid = _uid(row)
        low_thumb, _hi_thumb = thumb_urls_from_row(row)
        sel_cb = ft.Checkbox(
            value=uid in self._selected_uids,
            tooltip="Shift+click to select a range",
            on_change=lambda e, u=uid: self._on_item_check(u, bool(e.control.value)),
        )
        if uid:
            self._sel_cb_by_uid[uid] = sel_cb
        cells = [ft.Container(width=_SEL_COL_W, content=sel_cb, padding=2)]
        for c in cols:
            w = _COL_WIDTHS.get(c, 100)
            if c == "Thumbnail":
                low, hi = thumb_urls_from_row(row)
                box, img = make_lod_thumb(low, hi, size=52)
                if img is not None:
                    self._lod.add(img, low, hi, lod_y0, lod_y1)
                cells.append(ft.Container(width=w, content=box, padding=2))
            elif c == "Name":
                url = _url(row)
                cells.append(ft.Container(width=w, content=ft.TextButton(text=_name(row)[:44], url=url, style=ft.ButtonStyle(padding=0)) if url else ft.Text(_name(row)[:44], size=11), padding=2))
            elif c == "Assign":
                cells.append(
                    ft.Container(
                        width=w,
                        content=ft.Column(
                            [
                                self._suggestion_chips(row, compact=True, max_width=w - 4),
                                self._nwm_buttons(uid),
                            ],
                            spacing=2,
                            tight=True,
                        ),
                        padding=2,
                    )
                )
            elif c == "Downloaded":
                on_disk = str(row.get(c) or "").lower() in {"yes", "y", "true", "1"}
                if on_disk:
                    cells.append(
                        ft.Container(
                            width=w,
                            alignment=ft.alignment.center,
                            padding=2,
                            content=ft.IconButton(
                                icon=ft.Icons.CHECK_CIRCLE,
                                icon_size=16,
                                icon_color=ft.Colors.GREEN_400,
                                tooltip="Open in Explorer",
                                style=ft.ButtonStyle(padding=0),
                                on_click=lambda e, u=uid: self._reveal_download(u),
                            ),
                        )
                    )
                else:
                    cells.append(ft.Container(width=w, content=_yes_icon(row.get(c)), alignment=ft.alignment.center, padding=2))
            elif c == "Downloadable":
                cells.append(ft.Container(width=w, content=_yes_icon(row.get(c)), alignment=ft.alignment.center, padding=2))
            elif c == "License":
                val = normalize_license_label(row.get(c))
                cells.append(
                    ft.Container(
                        width=w,
                        content=ft.Text(
                            val or "—",
                            size=10,
                            color=_LICENSE_COLOR if val else ft.Colors.GREY_500,
                            max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            tooltip=val or "License unknown",
                        ),
                        padding=2,
                    )
                )
            elif c == "Face Count":
                val = _fmt(row.get(c)) or "—"
                cells.append(
                    ft.Container(
                        width=w,
                        content=ft.Text(val, size=10, text_align=ft.TextAlign.RIGHT, no_wrap=True),
                        padding=2,
                        alignment=ft.alignment.center_right,
                    )
                )
            else:
                val = _fmt(row.get(c))
                tip = val if c in ("Categories", "Assigned Collection(s)", "Tags") and len(val) > 30 else None
                max_ch = 50
                cells.append(
                    ft.Container(
                        width=w,
                        content=ft.Text(
                            val[:max_ch],
                            size=10,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            tooltip=tip,
                            no_wrap=True,
                        ),
                        padding=2,
                    )
                )

        dl_color, dl_tip = download_icon_style(row_is_downloadable(row.get("Downloadable")))
        cells.append(
            ft.Container(
                width=_ACTIONS_W,
                content=ft.Row(
                    [
                        ft.IconButton(icon=ft.Icons.VIEW_IN_AR, icon_size=14, tooltip="Preview", on_click=lambda e, u=uid, n=_name(row), t=low_thumb: self._preview_one(u, n, t)),
                        ft.IconButton(
                            icon=ft.Icons.DOWNLOAD,
                            icon_size=14,
                            icon_color=dl_color,
                            tooltip=dl_tip,
                            on_click=lambda e, u=uid, n=_name(row): self._download_one(u, n),
                        ),
                    ],
                    spacing=0,
                ),
                padding=0,
            )
        )
        override = self._pending_by_uid.get(uid)
        is_pending, is_in, pend_lbl, already_lbl, is_unlisted = _row_collection_status(row, override)
        already_names = _csv_names(row.get("Already In Collection(s)"))
        n_in = len(already_names)
        if is_pending:
            badge_txt = f"→ {pend_lbl}"
            badge_data = "pending_in" if is_in else "pending"
            badge_bg = _STATUS_PENDING
        elif is_unlisted and n_in <= 1:
            badge_txt = "Unlisted"
            badge_data = "unlisted"
            badge_bg = _STATUS_UNLISTED
        else:
            badge_txt = ""
            badge_data = ""
            badge_bg = _STATUS_IN
        status_badge = ft.Container(
            content=ft.Text(badge_txt, size=9, weight=ft.FontWeight.BOLD, color="#111827", no_wrap=True),
            bgcolor=badge_bg,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            border_radius=4,
            visible=bool(badge_txt),
            data=badge_data,
            tooltip=_already_tooltip(row),
        )
        if uid:
            self._status_badge_by_uid[uid] = status_badge
        already_chips = self._already_in_chips_row(uid, already_names, compact=True)
        # Insert status after checkbox
        cells.insert(
            1,
            ft.Container(
                width=160,
                content=ft.Column(
                    [status_badge, already_chips],
                    spacing=2,
                    tight=True,
                ),
                padding=2,
            ),
        )
        border = _status_border(is_pending, is_in)
        if border is None:
            border = ft.border.only(bottom=ft.BorderSide(1, _ROW_BORDER))
        return ft.Container(
            height=96,
            bgcolor=_status_bg(is_pending, is_in, grid=False, even=even),
            border=border,
            data=uid,
            opacity=0.22 if uid in self._hidden_uids else 1.0,
            content=ft.Row(cells, spacing=_COL_SPACING, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )

    async def _upgrade_lod(self, token: int) -> None:
        # Kept for compatibility; viewport LOD is driven by ScrollLodDebouncer.
        if self._lod_debouncer and self._page:
            self._lod_debouncer.kick(self._page)
