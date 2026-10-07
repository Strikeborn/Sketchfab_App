from __future__ import annotations

import threading
import time

import flet as ft
import pandas as pd

from browse_dates import DATE_FILTER_OPTS, client_cutoff, normalize_preset_date_filter, row_within_cutoff
from browse_models import normalize_search_model
from browse_presets import load_presets
from content_filter import filter_rows
from search_filter import normalize_license_label
from ui.flet_safe import safe_update
from ui.scroll_drag import wrap_middle_drag_scroll
from ui.liked_style import download_icon_style, row_is_downloadable
from ui.thumb_lod import make_lod_thumb, ViewportLod, ScrollLodDebouncer
from ui.viewer_place import screen_rect_for_card_image

_PANEL = "#0f172a"
_MIN_COLS = 2
_MAX_COLS = 10
_DEFAULT_COLS = 4
_LIST_ROW_H = 76
_MIN_VISIBLE = 24
_MAX_SKIP_HIDDEN = 15
_MAX_SKIP_HIDDEN_LIKED = 200
_MAX_AUTO_PAGES = 12
_MAX_AUTO_PAGES_FILTERED = 20
_SCROLL_LOAD_THRESHOLD = 360
# Match Liked: Activity ~280 + padding. Old -640 was leftover from the removed viewer dock.
_GRID_SIDE_CHROME = 340
_GRID_GAP = 8
_MIN_CARD_W = 152
_FALLBACK_GRID_AVAIL = 1100
_META_COLOR = "#fef08a"
_RAIL_W = 40
# Flet opacity format: "#rrggbb,0.94" — 8-digit hex alphas do not apply as expected
_RAIL_TINT = ft.Colors.with_opacity(0.94, "#020617")
_TITLE_COLOR = "#ffffff"

_SORT_OPTS = [
    ("-viewCount", "Most viewed"),
    ("-likeCount", "Most liked"),
    ("-publishedAt", "Newest"),
    ("_downloadCount", "Most downloaded*"),
    ("", "Relevance"),
]
# API ignores sort_by=downloadCount — we enrich then sort the loaded page locally.
_LOCAL_DL_SORT = "_downloadCount"


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _norm_uid(uid) -> str:
    return str(uid or "").strip().lower()


def _fmt_count(v) -> str:
    if v is None:
        return "…"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "…"


def _stats_line(row: dict, *, compact: bool = False) -> str:
    views = int(row.get("viewCount") or 0)
    likes = int(row.get("likeCount") or 0)
    dl = row.get("downloadCount")
    dl_s = _fmt_count(dl)
    lic = normalize_license_label(row.get("License"))
    lic_bit = f" · {lic}" if lic else ""
    if compact:
        return f"{views:,} views · {likes:,} likes · {dl_s} DL{lic_bit}"
    return f"{views:,} views · {likes:,} likes · {dl_s} downloads{lic_bit}"


def _uid(row: pd.Series) -> str:
    return _fmt(row.get("UID"))


class BrowseTab(ft.Column):
    """Mirror Liked tab layout: compact top row, collapsed filters, expand ListView last."""

    def __init__(self, page: ft.Page | None = None):
        super().__init__(expand=True, spacing=4)
        self._page = page
        self._rows: list[dict] = []
        self._session_rows: list[dict] = []
        self._next_url: str | None = None
        self._prev_url: str | None = None
        self._workbook_liked_uids: set[str] = set()
        self._runtime_liked_uids: set[str] = set()
        self._session_unliked_uids: set[str] = set()
        self._liked_uids: set[str] = set()
        self._downloaded_uids: set[str] = set()
        self._disk_icon_by_uid: dict[str, ft.IconButton] = {}
        self._pinned_uids: set[str] = set()
        self._selected_uids: set[str] = set()
        self._on_selection_change = None
        self._like_btns: dict[str, ft.IconButton] = {}
        self._grid_cols = _DEFAULT_COLS
        self._view_mode = "grid"
        self._busy = False
        self._on_search = None
        self._on_like = None
        self._on_page = None
        self._on_preview = None
        self._on_download = None
        self._on_export = None
        self._on_save_preset = None
        self._on_delete_preset = None
        self._on_open_account = None

        self._query = ft.TextField(
            label="Search",
            hint_text="Keywords…",
            width=240,
            dense=True,
            text_size=12,
            on_submit=lambda e: self._trigger_search(),
        )
        self._sort_dd = ft.Dropdown(label="Sort", width=130, dense=True, value="-viewCount", options=[ft.dropdown.Option(v, t) for v, t in _SORT_OPTS])
        self._category_dd = ft.Dropdown(label="Category", width=150, dense=True, value="", options=[ft.dropdown.Option("", "(all)")])
        self._date_dd = ft.Dropdown(
            label="Uploaded",
            width=150,
            dense=True,
            value="",
            options=[ft.dropdown.Option(v, t) for v, t in DATE_FILTER_OPTS],
        )
        self._dl_only = ft.Checkbox(label="Downloadable", value=False)
        self._staff_only = ft.Checkbox(label="Staff pick", value=False)
        self._anim_only = ft.Checkbox(label="Animated", value=False)
        self._min_dl = ft.TextField(
            label="Min downloads",
            hint_text="e.g. 1000",
            width=120,
            dense=True,
            text_size=12,
            keyboard_type=ft.KeyboardType.NUMBER,
            tooltip="Keep models with at least this many downloads (after counts load)",
            on_submit=lambda e: self._on_local_filter_change(),
        )
        self._max_likes = ft.TextField(
            label="Max likes",
            hint_text="e.g. 500",
            width=110,
            dense=True,
            text_size=12,
            keyboard_type=ft.KeyboardType.NUMBER,
            tooltip="Hide models above this like count (client filter — useful on Most liked sort)",
            on_submit=lambda e: self._on_local_filter_change(),
        )
        self._hide_liked = ft.Checkbox(label="Hide liked", value=True, on_change=lambda e: self._on_local_filter_change())
        self._hide_nsfw = True
        self._hide_female = True
        self._hide_male = True
        self._date_filter_key = ""
        self._last_search_params: dict = {}
        self._match_estimate = ""
        self._match_truncated = False
        self._auto_fill_rounds = 0
        self._on_estimate = None
        self._on_enrich = None
        self._pages_loaded = 0
        self._lod = ViewportLod()
        self._lod_debouncer: ScrollLodDebouncer | None = None
        self._last_card_w = 0
        self._scroll_offset = 0.0
        self._viewport_h = 800.0
        self._rect_by_uid: dict[str, dict[str, int]] = {}
        self._rendered_uids: set[str] = set()
        self._load_more_cooldown_until = 0.0
        self._interaction_block_until = 0.0
        self._fetch_inflight_url: str | None = None
        self._prefetch_url: str | None = None
        self._prefetch_data: dict | None = None
        self._prefetch_inflight_url: str | None = None
        self._resort_render_gen = 0
        self._max_scroll_extent = 0.0
        self._auto_scroll = False
        self._auto_speed = 480.0
        self._auto_task_running = False
        self._empty_append_streak = 0
        self._preset_dd = ft.Dropdown(label="Preset", width=160, dense=True, value="", options=[ft.dropdown.Option("", "(none)")])
        self._preset_name = ft.TextField(label="Preset name", width=140, dense=True, hint_text="Save as…")
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_400)
        self._cols_slider = ft.Slider(
            min=float(_MIN_COLS),
            max=float(_MAX_COLS),
            divisions=_MAX_COLS - _MIN_COLS,
            value=float(_DEFAULT_COLS),
            width=110,
            height=28,
            label="{value}/row",
            on_change=self._on_cols_change,
        )
        self._view_seg = ft.SegmentedButton(
            selected={"grid"},
            segments=[
                ft.Segment(value="list", label=ft.Text("List"), icon=ft.Icon(ft.Icons.VIEW_LIST)),
                ft.Segment(value="grid", label=ft.Text("Grid"), icon=ft.Icon(ft.Icons.GRID_VIEW)),
            ],
            on_change=self._on_view_change,
        )
        self._prev_btn = ft.IconButton(icon=ft.Icons.CHEVRON_LEFT, tooltip="Previous API page (resets list)", on_click=lambda e: self._go_prev(), disabled=True)
        self._next_btn = ft.IconButton(icon=ft.Icons.CHEVRON_RIGHT, tooltip="Load next page", on_click=lambda e: self._go_next(), disabled=True)
        self._load_more_btn = ft.OutlinedButton("Load more", icon=ft.Icons.EXPAND_MORE, on_click=lambda e: self._request_load_more(), disabled=True)
        self._auto_btn = ft.IconButton(
            icon=ft.Icons.PLAY_ARROW,
            tooltip="Autoscroll",
            on_click=lambda e: self.toggle_autoscroll(),
        )
        self._auto_speed_slider = ft.Slider(
            min=120,
            max=4800,
            divisions=39,
            value=480,
            width=90,
            height=28,
            label="{value} px/s",
            tooltip="Autoscroll speed (up to 4800 px/s)",
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
            padding=10,
            bgcolor=_PANEL,
            border_radius=8,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._sort_dd,
                            self._category_dd,
                            self._date_dd,
                            self._min_dl,
                            self._max_likes,
                            self._dl_only,
                            self._staff_only,
                            self._anim_only,
                            self._hide_liked,
                            ft.VerticalDivider(width=1, color="#334155"),
                            self._preset_dd,
                            ft.IconButton(icon=ft.Icons.PLAY_ARROW, tooltip="Load preset", on_click=lambda e: self._load_preset()),
                            self._preset_name,
                            ft.IconButton(icon=ft.Icons.SAVE, tooltip="Save preset", on_click=lambda e: self._save_preset()),
                            ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Delete preset", on_click=lambda e: self._delete_preset()),
                        ],
                        spacing=8,
                        wrap=False,
                        scroll=ft.ScrollMode.AUTO,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=0,
                tight=True,
            ),
        )

        self._cols_row = ft.Row(
            [ft.Text("Cols", size=11, color=ft.Colors.GREY_500), self._cols_slider],
            spacing=4,
            visible=True,
        )

        self.pager = ft.Row(
            [
                self._prev_btn,
                self._next_btn,
                self._load_more_btn,
                ft.OutlinedButton("Export page", icon=ft.Icons.TABLE_ROWS, on_click=lambda e: self._export_page()),
                ft.OutlinedButton("Export session", icon=ft.Icons.SAVE_ALT, on_click=lambda e: self._export_session()),
            ],
            spacing=4,
            tight=True,
        )
        self.header_bar = ft.Container(visible=False)
        self.scroller = ft.ListView(expand=True, spacing=4, padding=4, on_scroll_interval=40)
        self._scroller_wrap = wrap_middle_drag_scroll(
            self.scroller,
            expand=True,
            on_scroll=self._on_scroller_scroll,
            on_offset=self._on_autoscroll_offset,
        )
        self._lod_debouncer = ScrollLodDebouncer(self._lod, self, wait_s=0.05, throttle_s=0.045)
        self._refresh_preset_options()
        self._show_placeholder("Click Search — loads most-viewed models (or enter a query).")

        self._filter_btn = ft.IconButton(
            icon=ft.Icons.FILTER_LIST,
            tooltip="Filters & presets",
            on_click=lambda e: self._toggle_filters(),
        )

        self._toolbar_row = ft.Row(
            [
                self._query,
                self._filter_btn,
                ft.ElevatedButton("Search", icon=ft.Icons.TRAVEL_EXPLORE, height=36, on_click=lambda e: self._trigger_search()),
                self.pager,
                self._auto_row,
                ft.Container(expand=True),
                self._cols_row,
                self._view_seg,
                self._status,
            ],
            spacing=6,
            wrap=False,
            scroll=ft.ScrollMode.AUTO,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        # Chrome-only update — toggling filters must NOT remount the card grid.
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

    @property
    def mounted(self) -> bool:
        return bool(getattr(self, "page", None))

    def _safe_update(self, *controls: ft.Control | None) -> None:
        if not self.mounted:
            return
        safe_update(*controls, fallback=self)

    def _toggle_filters(self) -> None:
        open_now = not self._filter_panel.visible
        self._filter_panel.visible = open_now
        self._filter_btn.icon_color = ft.Colors.LIGHT_BLUE_300 if open_now else None
        # Liked pattern — pg.update(multi) is unreliable in Flet 0.28.
        try:
            self._chrome.update()
        except Exception:
            try:
                self._filter_panel.update()
            except Exception:
                pass
        if open_now:
            try:
                self._scroller_wrap.update()
            except Exception:
                pass
        try:
            self._filter_btn.update()
        except Exception:
            pass

    def arm_fetch_timeout(self, url: str, *, seconds: float = 20.0) -> None:
        """Clear stuck scroll-fetch if API/UI never finishes."""
        if not self._page or not url:
            return
        token = (url, time.monotonic())
        self._fetch_timeout_token = token

        async def _watch() -> None:
            import asyncio

            await asyncio.sleep(seconds)
            if getattr(self, "_fetch_timeout_token", None) != token:
                return
            if self._fetch_inflight_url == url:
                self._fetch_inflight_url = None
                self._refresh_status()
                safe_update(self._status, self._load_more_btn)

        try:
            self._page.run_task(_watch)
        except Exception:
            pass

    def _show_placeholder(self, msg: str) -> None:
        self._rendered_uids.clear()
        self.scroller.controls = [
            ft.Container(padding=20, content=ft.Text(msg, size=13, color=ft.Colors.GREY_500))
        ]

    def set_page(self, page: ft.Page) -> None:
        self._page = page

    def on_tab_shown(self) -> None:
        if not self._page:
            return
        if not self._rows and not self._busy and self._on_search:
            if not getattr(self, "_auto_searched", False):
                self._auto_searched = True
                self._trigger_search()
            return
        if self._lod_debouncer:
            self._lod_debouncer.kick(
                self._page,
                scroll=self._scroll_offset,
                viewport=self._viewport_h,
                remount_delay=0.05,
            )

    def _rebuild_liked_uids(self) -> None:
        self._liked_uids = (
            set(self._workbook_liked_uids)
            | set(self._runtime_liked_uids)
        ) - set(self._session_unliked_uids)

    def _on_liked_uids_changed(self, prev: set[str]) -> None:
        if not self._rows:
            return
        added = self._liked_uids - prev
        if self._hide_liked.value and added:
            keep = self._scroll_offset
            pg = self._page or getattr(self, "page", None)

            async def _rerender() -> None:
                self._render()
                self._safe_update(self.scroller, self._status)
                if keep > 0:
                    try:
                        self.scroller.scroll_to(offset=keep, duration=0)
                    except Exception:
                        pass

            if pg:
                try:
                    pg.run_task(_rerender)
                    return
                except Exception:
                    pass
            self._render()
            self._safe_update(self.scroller, self._status)
            if keep > 0:
                try:
                    self.scroller.scroll_to(offset=keep, duration=0)
                except Exception:
                    pass
            return
        self._refresh_status()
        self._safe_update(self._status)

    def set_liked_uids(self, uids: set[str]) -> None:
        wb = {_norm_uid(u) for u in (uids or ()) if _norm_uid(u)}
        self._runtime_liked_uids -= wb
        prev = set(self._liked_uids)
        self._workbook_liked_uids = wb
        self._rebuild_liked_uids()
        self._on_liked_uids_changed(prev)

    def set_downloaded_uids(self, uids: set[str]) -> None:
        self._downloaded_uids = {str(u).strip().lower() for u in (uids or ()) if str(u).strip()}

    def paint_downloaded(self, uid: str, *, on_disk: bool = True) -> None:
        uid = str(uid or "").strip().lower()
        if not uid:
            return
        if on_disk:
            self._downloaded_uids.add(uid)
        else:
            self._downloaded_uids.discard(uid)
        btn = self._disk_icon_by_uid.get(uid)
        if btn is None:
            return
        btn.icon_color = ft.Colors.GREEN_300 if on_disk else "#94a3b8"
        btn.disabled = not on_disk
        btn.tooltip = "On disk" if on_disk else "Not on disk"
        try:
            if getattr(btn, "page", None):
                btn.update()
        except Exception:
            pass

    def _on_local_filter_change(self) -> None:
        if self._rows:
            self._auto_fill_rounds = 0
            self._empty_append_streak = 0
            self._refresh_status()
            self._render()
            self._safe_update(self.scroller, self._status)
            self._maybe_auto_fill()

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        self._hide_nsfw = hide_nsfw
        self._hide_female = hide_female
        self._hide_male = hide_male
        if self._rows:
            self._refresh_status()
            self._render()
            self._safe_update(self.scroller, self._status)

    def set_categories(self, categories: list[dict]) -> None:
        opts = [ft.dropdown.Option("", "(all)")]
        for c in categories:
            slug = c.get("slug") or ""
            name = c.get("name") or slug
            if slug:
                opts.append(ft.dropdown.Option(slug, name))
        self._category_dd.options = opts
        safe_update(self._category_dd)

    def set_callbacks(self, on_search, on_page, on_like, on_preview=None, on_download=None, on_export=None, on_save_preset=None, on_delete_preset=None, on_estimate=None, on_enrich=None, on_open_account=None) -> None:
        self._on_search = on_search
        self._on_page = on_page
        self._on_like = on_like
        self._on_preview = on_preview
        self._on_download = on_download
        self._on_export = on_export
        self._on_save_preset = on_save_preset
        self._on_delete_preset = on_delete_preset
        self._on_estimate = on_estimate
        self._on_enrich = on_enrich
        self._on_open_account = on_open_account

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

    def clear_selection(self, *, emit: bool = True) -> None:
        if not self._selected_uids:
            if emit:
                self._emit_selection()
            return
        self._selected_uids.clear()
        if self._rows:
            self._render()
            self.update()
        if emit:
            self._emit_selection()

    def selected_row_dict(self, uid: str) -> dict | None:
        if not uid:
            return None
        for row in self._rows:
            if str(row.get("UID") or "") == str(uid):
                return dict(row)
        return None

    def _emit_selection(self) -> None:
        if not self._on_selection_change:
            return
        uids = self.get_selected_uids()
        row = self.selected_row_dict(uids[0]) if len(uids) == 1 else None
        try:
            self._on_selection_change(uids, row)
        except Exception:
            pass

    def _on_item_check(self, uid: str, checked: bool) -> None:
        if not uid:
            return
        if checked:
            self._selected_uids.add(uid)
        else:
            self._selected_uids.discard(uid)
        self._emit_selection()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_status()

    def _refresh_preset_options(self) -> None:
        presets = load_presets()
        self._preset_dd.options = [ft.dropdown.Option("", "(none)")] + [ft.dropdown.Option(p["name"], p["name"]) for p in presets if p.get("name")]

    def _load_preset(self) -> None:
        name = self._preset_dd.value or ""
        if not name:
            return
        for p in load_presets():
            if p.get("name") == name:
                self._query.value = p.get("query") or ""
                self._sort_dd.value = p.get("sort_by") or "-viewCount"
                self._category_dd.value = p.get("category") or ""
                self._date_dd.value = normalize_preset_date_filter(p.get("date_filter") or "")
                self._dl_only.value = bool(p.get("downloadable"))
                self._staff_only.value = bool(p.get("staffpicked"))
                self._anim_only.value = bool(p.get("animated"))
                self._preset_name.value = name
                self.update()
                self._trigger_search()
                return

    def _save_preset(self) -> None:
        name = (self._preset_name.value or "").strip() or (self._preset_dd.value or "").strip()
        if not name or not self._on_save_preset:
            return
        self._on_save_preset({
            "name": name,
            **self.search_params(),
            "category": self._category_dd.value or "",
            "date_filter": self._date_dd.value or "",
        })

    def _delete_preset(self) -> None:
        name = self._preset_dd.value or ""
        if name and self._on_delete_preset:
            self._on_delete_preset(name)

    def on_presets_changed(self) -> None:
        self._refresh_preset_options()
        self.update()

    def clear_prefetch(self) -> None:
        self._prefetch_url = None
        self._prefetch_data = None
        self._prefetch_inflight_url = None
        self._fetch_inflight_url = None

    def _trigger_search(self) -> None:
        if not self._on_search or self._busy:
            return
        self.set_busy(True)
        self.clear_prefetch()
        self._session_rows = []
        self._pinned_uids.clear()
        self._session_unliked_uids.clear()
        self._rows = []
        self._pages_loaded = 0
        self._auto_fill_rounds = 0
        self._match_estimate = ""
        self._match_truncated = False
        self._show_placeholder("Searching…")
        self._safe_update(self.scroller, self._status)
        self._on_search(cursor_url=None, append=False)

    def _go_next(self) -> None:
        if self._next_url and self._on_page and not self._busy:
            self._on_page(self._next_url, append=True)

    def _go_prev(self) -> None:
        if self._prev_url and self._on_page and not self._busy:
            self._on_page(self._prev_url, append=False)

    def _request_load_more(self) -> None:
        now = time.monotonic()
        if now < self._load_more_cooldown_until or now < self._interaction_block_until:
            return
        if self._fetch_inflight_url or not self._next_url or not self._on_page:
            return
        # Fresh scroll/nudge — allow another burst through liked-heavy API pages.
        self._empty_append_streak = 0
        self._load_more_cooldown_until = now + 0.12
        self._on_page(self._next_url, append=True)

    def _skip_hidden_limit(self) -> int:
        if self._hide_liked.value and self._liked_uids:
            return _MAX_SKIP_HIDDEN_LIKED
        return _MAX_SKIP_HIDDEN

    def _on_auto_speed(self, e) -> None:
        try:
            self._auto_speed = float(e.control.value or 480)
        except (TypeError, ValueError):
            self._auto_speed = 480.0

    def toggle_autoscroll(self, *, force: bool | None = None) -> None:
        self._auto_scroll = (not self._auto_scroll) if force is None else bool(force)
        self._auto_btn.icon = ft.Icons.PAUSE if self._auto_scroll else ft.Icons.PLAY_ARROW
        self._auto_btn.icon_color = ft.Colors.LIGHT_BLUE_300 if self._auto_scroll else None
        self._auto_btn.tooltip = "Stop autoscroll" if self._auto_scroll else "Autoscroll"
        self._safe_update(self._auto_btn)
        if self._auto_scroll and self._page:
            try:
                self._page.run_task(self._autoscroll_loop)
            except Exception:
                self._auto_scroll = False
                self._auto_task_running = False

    def _scroll_extent(self, *, allow_estimate: bool = True) -> float:
        """Prefer ListView-reported extent; estimate only before first layout."""
        real = float(self._max_scroll_extent or 0)
        if real > 0 or not allow_estimate:
            return real
        return self._estimate_scroll_extent()

    def _near_scroll_bottom(self, offset: float | None = None, *, allow_estimate: bool = True) -> bool:
        off = float(self._scroll_offset if offset is None else offset)
        vh = float(self._viewport_h or 800)
        extent = self._scroll_extent(allow_estimate=allow_estimate)
        if extent <= 0:
            return False
        return (off + vh) >= (extent - _SCROLL_LOAD_THRESHOLD)

    async def _autoscroll_loop(self) -> None:
        import asyncio
        import time

        if self._auto_task_running:
            return
        self._auto_task_running = True
        try:
            idle_ticks = 0
            load_tick = 0
            last_t = time.monotonic()
            while self._auto_scroll and self._page:
                now = time.monotonic()
                dt = min(0.04, max(0.001, now - last_t))
                last_t = now
                step = max(4.0, float(self._auto_speed) * dt)
                prev = float(self._scroll_offset or 0)
                load_tick += 1
                if load_tick >= 4:
                    load_tick = 0
                    if self._next_url and not self._fetch_inflight_url:
                        extent = self._scroll_extent(allow_estimate=False)
                        if extent <= 0 or (prev + self._viewport_h) >= (
                            extent - _SCROLL_LOAD_THRESHOLD
                        ):
                            try:
                                self._request_load_more()
                            except Exception:
                                pass
                try:
                    self.scroller.scroll_to(
                        delta=step,
                        duration=max(1, int(dt * 1000)),
                        curve=ft.AnimationCurve.LINEAR,
                    )
                except Exception:
                    break
                await asyncio.sleep(0.016)
                cur = float(self._scroll_offset or 0)
                extent = float(self._max_scroll_extent or 0)
                n_visible = len(self._filter_rows())
                at_end = (
                    n_visible > 0
                    and not self._next_url
                    and extent > 0
                    and cur + 32 >= extent
                )
                # Do not treat lagging scroll events as idle — that stopped autoscroll instantly.
                if self._fetch_inflight_url or self._next_url:
                    idle_ticks = 0
                elif at_end:
                    idle_ticks += 1
                    if idle_ticks >= 8:
                        self._auto_scroll = False
                        break
                else:
                    idle_ticks = 0
        finally:
            self._auto_task_running = False
            if self._lod_debouncer and self._page:
                try:
                    self._lod_debouncer.kick(
                        self._page,
                        scroll=self._scroll_offset,
                        viewport=self._viewport_h,
                        remount_delay=0.04,
                    )
                except Exception:
                    pass
            if not self._auto_scroll:
                self._auto_btn.icon = ft.Icons.PLAY_ARROW
                self._auto_btn.icon_color = None
                self._auto_btn.tooltip = "Autoscroll"
                self._safe_update(self._auto_btn)

    def _estimate_scroll_extent(self) -> float:
        lv_gap = 4.0  # ListView spacing
        if self._view_mode == "grid":
            cols = max(1, self._effective_cols())
            n = len(self._rendered_uids)
            if n <= 0:
                return 0.0
            rows = (n + cols - 1) // cols
            _, thumb_h = self._card_dims()
            row_h = float(thumb_h) + 6.0 + lv_gap
            return rows * row_h
        n = len(self._rendered_uids)
        return max(0.0, n * (_LIST_ROW_H + lv_gap))

    def _sync_scroll_extent(self) -> None:
        """No-op — inflating max_scroll_extent from estimates caused autoscroll bounce."""
        return

    def _bump_scroll(self, delta: float) -> None:
        self._scroll_offset = max(0.0, float(self._scroll_offset or 0) + float(delta))
        self._sync_scroll_extent()
        self._on_autoscroll_offset(self._scroll_offset)

    def _client_filters_active(self) -> bool:
        if self._hide_liked.value and self._liked_uids:
            return True
        if self._max_like_count() > 0:
            return True
        if self._min_downloads() > 0:
            return True
        if self._hide_nsfw or self._hide_female or self._hide_male:
            return True
        return False

    def _visible_target(self) -> int:
        """Enough cards to fill ~2 screens plus prerender buffer."""
        vh = float(self._viewport_h or 800)
        if self._view_mode == "grid":
            cols = max(1, self._effective_cols())
            _, thumb_h = self._card_dims()
            row_h = max(80.0, float(thumb_h) + 10.0)
            rows_screen = max(2, int(vh / row_h) + 1)
            return max(_MIN_VISIBLE, cols * rows_screen * 3)
        rows_screen = max(3, int(vh / (_LIST_ROW_H + 4.0)) + 1)
        return max(_MIN_VISIBLE, rows_screen * 3)

    def _auto_fill_limit(self) -> int:
        if self._client_filters_active():
            return _MAX_AUTO_PAGES_FILTERED
        return _MAX_AUTO_PAGES

    def _maybe_auto_fill(self) -> None:
        """Keep fetching until client filters leave enough visible rows to scroll."""
        if self._fetch_inflight_url or not self._next_url or not self._on_page:
            return
        shown = len(self._filter_rows())
        if shown >= self._visible_target():
            return
        if self._auto_fill_rounds >= self._auto_fill_limit():
            return
        self._auto_fill_rounds += 1
        self._on_page(self._next_url, append=True)

    def _maybe_skip_hidden_page(self) -> None:
        """Zero new visible cards on append — count toward skip streak, then auto-fill."""
        if not self._next_url or not self._on_page or self._fetch_inflight_url:
            return
        limit = self._skip_hidden_limit()
        if self._empty_append_streak >= limit:
            if self._auto_scroll or len(self._filter_rows()) < self._visible_target():
                self._empty_append_streak = 0
            else:
                return
        self._empty_append_streak += 1
        self._maybe_auto_fill()

    def _on_autoscroll_offset(self, offset: float) -> None:
        self._scroll_offset = float(offset or 0)
        if self._auto_scroll:
            return
        if self._lod_debouncer and self._page:
            self._lod_debouncer.nudge(self._page, self._scroll_offset, self._viewport_h)

    def _on_scroller_scroll(self, e: ft.OnScrollEvent) -> None:
        try:
            self._scroll_offset = float(e.pixels or 0)
            self._viewport_h = float(e.viewport_dimension or self._viewport_h or 800)
            real = float(e.max_scroll_extent or 0)
            if real > 0:
                self._max_scroll_extent = real
        except (TypeError, ValueError):
            pass
        if self._lod_debouncer and self._page and not self._auto_scroll:
            self._lod_debouncer.on_scroll(e, self._page)
        if self._fetch_inflight_url or not self._next_url:
            return
        dragging = getattr(self._scroller_wrap, "is_middle_dragging", None)
        if callable(dragging) and dragging():
            return
        if self._near_scroll_bottom(allow_estimate=True):
            self._request_load_more()

    def _export_page(self) -> None:
        rows = self._filter_rows()
        if rows and self._on_export:
            self._on_export(rows, self._query.value or "", session=False)

    def _export_session(self) -> None:
        if self._session_rows and self._on_export:
            self._on_export(self._session_rows, self._query.value or "", session=True)

    def _on_cols_change(self, e) -> None:
        self._grid_cols = int(round(float(e.control.value or _DEFAULT_COLS)))
        if self._rows:
            self._render()
            self.update()

    def _on_view_change(self, e) -> None:
        sel = getattr(e.control, "selected", None) or {"grid"}
        if isinstance(sel, set) and sel:
            self._view_mode = next(iter(sel))
        self._cols_row.visible = self._view_mode == "grid"
        if self._rows:
            self._render()
            self.update()

    def _min_downloads(self) -> int:
        raw = (self._min_dl.value or "").strip().replace(",", "")
        if not raw:
            return 0
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            return 0

    def _max_like_count(self) -> int:
        raw = (self._max_likes.value or "").strip().replace(",", "")
        if not raw:
            return 0
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            return 0

    def _filter_rows(self) -> list[dict]:
        rows = list(self._rows)
        if self._hide_liked.value and self._liked_uids:
            rows = [
                r for r in rows
                if _norm_uid(r.get("UID")) not in self._liked_uids
                or _norm_uid(r.get("UID")) in self._pinned_uids
            ]
        max_likes = self._max_like_count()
        if max_likes > 0:
            kept = []
            for r in rows:
                try:
                    if int(r.get("likeCount") or 0) <= max_likes:
                        kept.append(r)
                except (TypeError, ValueError):
                    kept.append(r)
            rows = kept
        min_dl = self._min_downloads()
        if min_dl > 0:
            kept = []
            for r in rows:
                dc = r.get("downloadCount")
                if dc is None:
                    continue  # hide until enriched
                try:
                    if int(dc) >= min_dl:
                        kept.append(r)
                except (TypeError, ValueError):
                    continue
            rows = kept
        rows = filter_rows(rows, hide_nsfw=self._hide_nsfw, hide_female=self._hide_female, hide_male=self._hide_male)
        if (self._sort_dd.value or "") == _LOCAL_DL_SORT:
            def _dl_key(r):
                dc = r.get("downloadCount")
                try:
                    return int(dc) if dc is not None else -1
                except (TypeError, ValueError):
                    return -1
            rows = sorted(rows, key=_dl_key, reverse=True)
        return rows

    def mark_liked(self, uid: str) -> None:
        """Keep row on screen until next search; update heart icon only."""
        uid = _norm_uid(uid)
        if not uid:
            return
        self._session_unliked_uids.discard(uid)
        self._runtime_liked_uids.add(uid)
        self._rebuild_liked_uids()
        self._pinned_uids.add(uid)
        self._paint_like_btn(uid, liked=True)

    def revert_optimistic_like(self, uid: str) -> None:
        """Undo a failed like API call — keep heart if workbook already had this UID."""
        uid = _norm_uid(uid)
        if not uid or uid in self._workbook_liked_uids:
            return
        self._runtime_liked_uids.discard(uid)
        self._pinned_uids.discard(uid)
        self._rebuild_liked_uids()
        self._paint_like_btn(uid, liked=False)

    def mark_unliked(self, uid: str) -> None:
        uid = _norm_uid(uid)
        if not uid:
            return
        self._session_unliked_uids.add(uid)
        self._runtime_liked_uids.discard(uid)
        self._rebuild_liked_uids()
        self._pinned_uids.discard(uid)
        self._paint_like_btn(uid, liked=False)
        try:
            self._refresh_status()
            if getattr(self, "page", None) and getattr(self._status, "page", None):
                self._status.update()
        except Exception:
            pass

    def _paint_like_btn(self, uid: str, *, liked: bool) -> None:
        btn = self._like_btns.get(uid)
        if not btn:
            try:
                self._refresh_status()
                if getattr(self, "page", None) and getattr(self._status, "page", None):
                    self._status.update()
            except Exception:
                pass
            return
        btn.icon = ft.Icons.FAVORITE if liked else ft.Icons.FAVORITE_BORDER
        btn.icon_color = "#f9a8d4" if liked else "#ffffff"
        btn.disabled = not uid
        btn.tooltip = "Unlike" if liked else "Like"
        try:
            if getattr(btn, "page", None):
                btn.update()
                return
        except Exception:
            pass
        try:
            self._refresh_status()
            if getattr(self, "page", None) and getattr(self._status, "page", None):
                self._status.update()
        except Exception:
            pass

    def _browse_thumb(self, row: dict, size: int, *, lod_y0: float = 0.0, lod_y1: float = 0.0) -> tuple[ft.Container, ft.Image | None, str, str]:
        low = row.get("Thumbnail") or ""
        hi = row.get("Thumbnail HD") or low
        box, img = make_lod_thumb(low or hi, hi or low, size=size)
        if img is not None:
            self._lod.add(img, low or hi, hi or low, lod_y0, lod_y1 or (lod_y0 + size))
        return box, img, low or hi, hi or low

    def set_match_estimate(self, count: int, *, truncated: bool = False) -> None:
        if count < 0:
            self._match_estimate = ""
            self._match_truncated = False
        else:
            self._match_estimate = f"{count:,}+" if truncated else f"{count:,}"
            self._match_truncated = truncated
        self._refresh_status()
        self._safe_update(self._status)

    def patch_download_counts(self, counts: dict[str, int]) -> None:
        if not counts:
            return
        changed = False
        for row in self._rows:
            uid = row.get("UID")
            if uid and uid in counts:
                row["downloadCount"] = counts[uid]
                changed = True
        for row in self._session_rows:
            uid = row.get("UID")
            if uid and uid in counts:
                row["downloadCount"] = counts[uid]
        if not changed:
            return
        self._refresh_status()
        if self._needs_resort_render():
            self._schedule_resort_render()
        else:
            try:
                if getattr(self._status, "page", None):
                    self._status.update()
            except Exception:
                pass
        self._maybe_auto_fill()

    def _needs_resort_render(self) -> bool:
        return (self._sort_dd.value or "") == _LOCAL_DL_SORT or self._min_downloads() > 0

    def _schedule_resort_render(self) -> None:
        if not self._page:
            return
        self._resort_render_gen += 1
        gen = self._resort_render_gen

        async def _later():
            import asyncio

            await asyncio.sleep(0.25)
            if gen != self._resort_render_gen or not self._rows:
                return
            keep = self._scroll_offset
            self._render()
            self._safe_update(self.scroller)
            if keep > 0:
                try:
                    self.scroller.scroll_to(offset=keep, duration=0)
                except Exception:
                    pass

        try:
            self._page.run_task(_later)
        except Exception:
            pass

    def uids_needing_download_count(self) -> list[str]:
        out: list[str] = []
        for row in self._rows:
            uid = row.get("UID")
            if uid and row.get("downloadCount") is None:
                out.append(uid)
        return out

    def _refresh_status(self) -> None:
        shown = len(self._filter_rows())
        loaded = len(self._rows)
        sess = len(self._session_rows)
        cutoff = client_cutoff(self._date_filter_key)
        parts = [f"{shown:,} shown", f"{loaded:,} loaded"]
        if self._hide_liked.value and self._liked_uids and loaded > shown:
            parts.append(f"{loaded - shown:,} hidden (liked)")
        max_lk = self._max_like_count()
        if max_lk > 0:
            parts.append(f"≤{max_lk:,} likes")
        if self._match_estimate:
            label = f"~{self._match_estimate} API" if self._match_truncated else f"{self._match_estimate} API"
            parts.append(label)
        if self._next_url:
            parts.append("more available")
            target = self._visible_target()
            if shown < target:
                if self._fetch_inflight_url:
                    parts.append(f"fetching ahead ({shown}/{target})")
                elif self._auto_fill_rounds >= self._auto_fill_limit():
                    parts.append("Load more to continue")
            elif (
                self._hide_liked.value
                and self._empty_append_streak >= self._skip_hidden_limit()
            ):
                parts.append("scroll to fetch ahead")
        elif loaded:
            parts.append("API end")
        parts.append(f"session {sess:,}")
        if cutoff is not None:
            parts.append("date filter")
        self._status.value = " · ".join(parts)
        self._load_more_btn.disabled = not bool(self._next_url) or bool(self._fetch_inflight_url)

    def set_api_page(self, api_data: dict, *, append: bool = False) -> None:
        raw = api_data.get("results") or []
        new_rows = [normalize_search_model(m) for m in raw]
        cutoff = client_cutoff(self._date_filter_key)
        if cutoff is not None:
            new_rows = [r for r in new_rows if row_within_cutoff(r, cutoff)]

        if append:
            seen = {r.get("UID") for r in self._rows}
            for r in new_rows:
                uid = r.get("UID")
                if uid and uid not in seen:
                    self._rows.append(r)
                    seen.add(uid)
            keep_scroll = self._scroll_offset
        else:
            self._rows = new_rows
            self._selected_uids.clear()
            self._emit_selection()
            self._pages_loaded = 0
            self._auto_fill_rounds = 0
            self._empty_append_streak = 0
            keep_scroll = 0.0
            try:
                self.scroller.scroll_to(offset=0, duration=0)
            except Exception:
                pass
            self._scroll_offset = 0.0

        self._pages_loaded += 1
        seen_sess = {r.get("UID") for r in self._session_rows}
        for r in new_rows:
            uid = r.get("UID")
            if uid and uid not in seen_sess:
                self._session_rows.append(r)
                seen_sess.add(uid)

        self._next_url = api_data.get("next")
        self._prev_url = api_data.get("previous")
        self._prev_btn.disabled = not bool(self._prev_url)
        self._next_btn.disabled = not bool(self._next_url)
        self._refresh_status()
        if append:
            appended = self._append_render()
            self._sync_scroll_extent()
            self._safe_update(self.scroller, self._status)
            if appended:
                self._empty_append_streak = 0
            elif self._next_url:
                self._maybe_skip_hidden_page()
            self._maybe_auto_fill()
        else:
            self._render()
            self._safe_update(self.scroller, self._status)
            self._maybe_auto_fill()
        if append and keep_scroll > 0:
            try:
                self.scroller.scroll_to(offset=keep_scroll, duration=0)
            except Exception:
                pass
            self._scroll_offset = keep_scroll
        if self._lod_debouncer and self._page:
            self._lod_debouncer.kick(
                self._page,
                scroll=self._scroll_offset,
                viewport=self._viewport_h,
                remount_delay=0.03 if append else 0.08,
            )
        if self._on_enrich and self._needs_resort_render():
            if append:
                uids = [
                    str(r.get("UID") or "").strip()
                    for r in new_rows
                    if r.get("UID") and r.get("downloadCount") is None
                ]
            else:
                uids = self.uids_needing_download_count()
            if uids:
                self._on_enrich(uids)

    def _card_dims(self) -> tuple[int, int]:
        avail = self._grid_avail_width()
        cols = self._effective_cols(avail)
        card_w = max(_MIN_CARD_W, int((avail - _GRID_GAP * (cols - 1)) / cols))
        return card_w, max(120, card_w - 4)

    def _grid_avail_width(self) -> int:
        page_w = 0
        if self._page is not None:
            try:
                page_w = int(float(self._page.width or 0))
            except (TypeError, ValueError):
                page_w = 0
        if page_w >= 1000:
            return max(520, page_w - _GRID_SIDE_CHROME)
        return _FALLBACK_GRID_AVAIL

    def _effective_cols(self, avail: int | None = None) -> int:
        avail = int(avail if avail is not None else self._grid_avail_width())
        cols = max(_MIN_COLS, min(_MAX_COLS, int(self._grid_cols or _DEFAULT_COLS)))
        while cols > 1 and (avail - _GRID_GAP * (cols - 1)) / max(cols, 1) < _MIN_CARD_W:
            cols -= 1
        return max(1, cols)

    def on_page_resize(self, width: float | None = None) -> None:
        if self._view_mode != "grid" or not self._rows:
            return
        prev = int(self._last_card_w or 0)
        card_w, _ = self._card_dims()
        if prev and abs(card_w - prev) < 10:
            return
        self._last_card_w = card_w
        keep = self._scroll_offset
        self._render()
        self._safe_update(self.scroller)
        if keep > 0:
            try:
                self.scroller.scroll_to(offset=keep, duration=0)
            except Exception:
                pass

    def on_viewport_sync(self) -> None:
        if self._lod_debouncer and self._page and not self._auto_scroll:
            self._lod_debouncer.sync_now(allow_downgrade=False)

    def _append_render(self) -> bool:
        """Add new cards/rows at the bottom — do not rebuild existing controls."""
        visible = self._filter_rows()
        new_items = [
            r for r in visible
            if (uid := _norm_uid(r.get("UID"))) and uid not in self._rendered_uids
        ]
        if not new_items:
            return False

        # Placeholder or broken state — fall back to full render.
        if not self.scroller.controls or not hasattr(self.scroller.controls[0], "content"):
            self._render()
            return bool(self._rendered_uids)

        start_index = len(self._rendered_uids)
        if self._view_mode == "grid":
            self._append_grid_cards(new_items, start_index=start_index)
        else:
            self._append_list_rows(new_items, start_index=start_index)

        for r in new_items:
            uid = _norm_uid(r.get("UID"))
            if uid:
                self._rendered_uids.add(uid)

        if self._lod_debouncer and self._page:
            self._lod_debouncer.kick(
                self._page,
                scroll=self._scroll_offset,
                viewport=self._viewport_h,
                remount_delay=0.08,
            )
        return True

    def _append_grid_cards(self, new_items: list[dict], *, start_index: int) -> None:
        cols_n = self._effective_cols()
        card_w, thumb_h = self._card_dims()
        card_h = float(thumb_h)
        gap = float(_GRID_GAP)

        for j, r in enumerate(new_items):
            i = start_index + j
            row_i = i // cols_n
            col_i = i % cols_n
            y0 = row_i * (card_h + gap)
            card = self._grid_card(r, lod_y0=y0, lod_y1=y0 + card_h)
            if col_i == 0:
                self.scroller.controls.append(
                    ft.Container(
                        padding=ft.padding.only(bottom=6),
                        height=card_h + 6,
                        content=ft.Row([card], spacing=_GRID_GAP, wrap=False),
                    )
                )
            else:
                row_container = self.scroller.controls[-1]
                row_content = getattr(row_container, "content", None)
                if not isinstance(row_content, ft.Row):
                    self._render()
                    return
                row_content.controls.append(card)

    def _append_list_rows(self, new_items: list[dict], *, start_index: int) -> None:
        gap = 4.0
        row_h = float(_LIST_ROW_H)
        base = start_index
        for j, r in enumerate(new_items):
            i = base + j
            self.scroller.controls.append(
                self._list_row(
                    r,
                    i % 2 == 0,
                    lod_y0=i * (row_h + gap),
                    lod_y1=i * (row_h + gap) + row_h,
                )
            )

    def _render(self) -> None:
        self._like_btns = {}
        self._disk_icon_by_uid = {}
        self._lod.clear()
        self._rendered_uids.clear()
        rows = self._filter_rows()
        if not rows:
            if self._rows and self._hide_liked.value:
                self._show_placeholder("All results already in your likes — uncheck 'Hide liked' in Filters.")
            elif self._rows and (self._hide_nsfw or self._hide_female or self._hide_male):
                self._show_placeholder("All results hidden by N / W / M toolbar filters.")
            elif self._rows:
                self._show_placeholder("No results match current filters.")
            else:
                if self._busy:
                    self._show_placeholder("Searching…")
                else:
                    self._show_placeholder("Click Search — loads most-viewed models (or enter a query).")
            return

        if self._view_mode == "grid":
            card_w, thumb_h = self._card_dims()
            cols_n = self._effective_cols()
            card_h = float(thumb_h)
            gap = float(_GRID_GAP)
            cards = []
            for i, r in enumerate(rows):
                row_i = i // cols_n
                y0 = row_i * (card_h + gap)
                cards.append(self._grid_card(r, lod_y0=y0, lod_y1=y0 + card_h))
            grid_rows: list[ft.Control] = []
            for i in range(0, len(cards), cols_n):
                grid_rows.append(
                    ft.Container(
                        padding=ft.padding.only(bottom=6),
                        height=card_h + 6,
                        content=ft.Row(cards[i : i + cols_n], spacing=_GRID_GAP, wrap=False),
                    )
                )
            self.scroller.controls = grid_rows
            for r in rows:
                uid = _norm_uid(r.get("UID"))
                if uid:
                    self._rendered_uids.add(uid)
        else:
            gap = 4.0
            row_h = float(_LIST_ROW_H)
            self.scroller.controls = [
                self._list_row(r, i % 2 == 0, lod_y0=i * (row_h + gap), lod_y1=i * (row_h + gap) + row_h)
                for i, r in enumerate(rows)
            ]
            for r in rows:
                uid = _norm_uid(r.get("UID"))
                if uid:
                    self._rendered_uids.add(uid)
        if self._lod_debouncer and self._page:
            self._lod_debouncer.kick(
                self._page,
                scroll=self._scroll_offset,
                viewport=self._viewport_h,
                remount_delay=0.08,
            )

    def _list_row(self, row: dict, even: bool, *, lod_y0: float = 0.0, lod_y1: float = 76.0) -> ft.Container:
        thumb_box, _img, _low, _hi = self._browse_thumb(row, 56, lod_y0=lod_y0, lod_y1=lod_y1)
        uid = _norm_uid(row.get("UID"))
        name = _fmt(row.get("Name"))
        author = _fmt(row.get("Author"))
        author_key = _fmt(row.get("Author Username")) or author
        viewer = row.get("viewerUrl") or ""
        url = f"https://sketchfab.com/3d-models/{uid}" if uid else None
        liked = uid in self._liked_uids
        dl = str(row.get("Downloadable", "")).lower() == "yes"
        on_disk = bool(uid) and uid.lower() in self._downloaded_uids
        stats = _stats_line(row)
        title = ft.TextButton(text=name, url=url, style=ft.ButtonStyle(padding=0)) if url else ft.Text(name, size=12, weight=ft.FontWeight.W_500)
        like_btn = ft.IconButton(
            icon=ft.Icons.FAVORITE if liked else ft.Icons.FAVORITE_BORDER,
            icon_size=18,
            tooltip="Unlike" if liked else "Like",
            disabled=not uid,
            on_click=lambda e, u=uid: self._toggle_like(u),
        )
        if uid:
            self._like_btns[uid] = like_btn
        disk_btn = ft.IconButton(
            icon=ft.Icons.SAVE_ALT,
            icon_size=16,
            icon_color=ft.Colors.GREEN_300 if on_disk else "#94a3b8",
            tooltip="On disk" if on_disk else "Not on disk",
            disabled=not on_disk,
        )
        if uid:
            self._disk_icon_by_uid[uid.lower()] = disk_btn
        dl_color, dl_tip = download_icon_style(dl)
        dl_btn = ft.IconButton(
            icon=ft.Icons.DOWNLOAD_FOR_OFFLINE,
            icon_size=18,
            icon_color=dl_color,
            tooltip=dl_tip,
            on_click=lambda e, u=uid, n=name: self._download_one(u, n),
        )

        author_ctrl = (
            ft.TextButton(
                text=author,
                style=ft.ButtonStyle(padding=0, color=_META_COLOR),
                tooltip=f"Open @{author_key} in Account",
                on_click=lambda e, k=author_key: self._on_open_account(k) if self._on_open_account else None,
            )
            if author and author_key and self._on_open_account
            else None
        )
        meta_row = ft.Row(
            [
                c
                for c in (
                    author_ctrl,
                    ft.Text("·", size=10, color=_META_COLOR) if author_ctrl else None,
                    ft.Text(stats, size=10, color=_META_COLOR, max_lines=1, expand=True),
                )
                if c is not None
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ) if author else ft.Text(stats, size=10, color=_META_COLOR, max_lines=1)

        sel_cb = ft.Checkbox(
            value=uid in self._selected_uids,
            on_change=lambda e, u=uid: self._on_item_check(u, bool(e.control.value)),
        )
        return ft.Container(
            height=_LIST_ROW_H,
            bgcolor="#111827" if even else "#1a2332",
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            border=ft.border.only(bottom=ft.BorderSide(1, "#334155")),
            content=ft.Row(
                [
                    sel_cb,
                    thumb_box,
                    ft.Column(
                        [
                            title,
                            meta_row,
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.IconButton(icon=ft.Icons.VIEW_IN_AR, icon_size=18, tooltip="Preview", on_click=lambda e, u=uid, n=name, v=viewer: self._preview_one(u, n, v)),
                    dl_btn,
                    disk_btn,
                    like_btn,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _grid_card(self, row: dict, *, lod_y0: float = 0.0, lod_y1: float = 0.0) -> ft.Container:
        card_w, thumb_h = self._card_dims()
        thumb_box, img, low, hi = self._browse_thumb(row, thumb_h, lod_y0=lod_y0, lod_y1=lod_y1 or (lod_y0 + thumb_h))
        uid = _norm_uid(row.get("UID"))
        name = _fmt(row.get("Name"))
        author = _fmt(row.get("Author"))
        author_key = _fmt(row.get("Author Username")) or author
        viewer = row.get("viewerUrl") or ""
        url = f"https://sketchfab.com/3d-models/{uid}" if uid else None
        liked = uid in self._liked_uids
        dl = str(row.get("Downloadable", "")).lower() == "yes"
        on_disk = bool(uid) and uid.lower() in self._downloaded_uids
        stats = _stats_line(row, compact=True)
        like_btn = ft.IconButton(
            icon=ft.Icons.FAVORITE if liked else ft.Icons.FAVORITE_BORDER,
            icon_size=18,
            icon_color="#f9a8d4" if liked else "#ffffff",
            tooltip="Unlike" if liked else "Like",
            disabled=not uid,
            style=ft.ButtonStyle(padding=0),
            on_click=lambda e, u=uid: self._toggle_like(u),
        )
        if uid:
            self._like_btns[uid] = like_btn

        disk_btn = ft.IconButton(
            icon=ft.Icons.SAVE_ALT,
            icon_size=16,
            icon_color=ft.Colors.GREEN_300 if on_disk else "#94a3b8",
            tooltip="On disk" if on_disk else "Not on disk",
            disabled=not on_disk,
            style=ft.ButtonStyle(padding=0),
        )
        if uid:
            self._disk_icon_by_uid[uid.lower()] = disk_btn

        title = (
            ft.TextButton(
                text=name[: max(20, card_w // 6)],
                url=url,
                style=ft.ButtonStyle(padding=0, color="#ffffff"),
            )
            if url
            else ft.Text(
                name[: max(20, card_w // 6)],
                size=12,
                weight=ft.FontWeight.W_700,
                color="#ffffff",
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
        )
        author_ctrl = (
            ft.TextButton(
                text=author,
                style=ft.ButtonStyle(padding=0, color=_META_COLOR),
                tooltip=f"Open @{author_key} in Account",
                on_click=lambda e, k=author_key: self._on_open_account(k) if self._on_open_account else None,
            )
            if author and author_key and self._on_open_account
            else (ft.Text(author, size=10, weight=ft.FontWeight.W_700, color=_META_COLOR) if author else None)
        )
        meta_ctrl = ft.Column(
            [
                c
                for c in (
                    author_ctrl,
                    ft.Text(
                        stats,
                        size=10,
                        weight=ft.FontWeight.W_700,
                        color=_META_COLOR,
                        max_lines=2,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                )
                if c is not None
            ],
            spacing=0,
            tight=True,
        )

        sel_cb = ft.Checkbox(
            value=uid in self._selected_uids,
            scale=0.85,
            on_change=lambda e, u=uid: self._on_item_check(u, bool(e.control.value)),
        )
        rail_kids: list[ft.Control] = [
            ft.Container(content=sel_cb, alignment=ft.alignment.center),
            ft.IconButton(
                icon=ft.Icons.VIEW_IN_AR,
                icon_size=18,
                icon_color="#67e8f9",
                tooltip="Preview",
                style=ft.ButtonStyle(padding=0),
                on_click=lambda e, u=uid, n=name, v=viewer: self._preview_one(u, n, v),
            ),
        ]
        dl_color, dl_tip = download_icon_style(dl)
        rail_kids.append(
            ft.IconButton(
                icon=ft.Icons.DOWNLOAD_FOR_OFFLINE,
                icon_size=18,
                icon_color=dl_color,
                tooltip=dl_tip,
                style=ft.ButtonStyle(padding=0),
                on_click=lambda e, u=uid, n=name: self._download_one(u, n),
            )
        )
        rail_kids.extend([disk_btn, like_btn])
        left_rail = ft.Container(
            left=0,
            top=0,
            bottom=0,
            width=_RAIL_W,
            bgcolor=_RAIL_TINT,
            padding=ft.padding.symmetric(vertical=4, horizontal=2),
            content=ft.Column(
                rail_kids,
                spacing=0,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        text_w = max(_RAIL_W + 8, min(card_w - 8, int(card_w * 0.72)))
        bottom_text = ft.Container(
            left=0,
            bottom=0,
            width=text_w,
            bgcolor=_RAIL_TINT,
            padding=ft.padding.only(left=_RAIL_W + 4, right=6, top=6, bottom=6),
            content=ft.Column(
                [
                    title,
                    meta_ctrl,
                ],
                spacing=2,
                tight=True,
            ),
        )

        def _on_enter(_e, u=uid, h=hi, im=img, cw=card_w, ch=thumb_h):
            if im is not None and h:
                try:
                    im.src = h
                    im.update()
                except Exception:
                    pass
            rect = screen_rect_for_card_image(self._page, _e, cw, ch, rail_w=_RAIL_W, meta_h=68)
            if u and rect:
                self._rect_by_uid[u] = rect

        def _on_hover(_e, u=uid, cw=card_w, ch=thumb_h):
            rect = screen_rect_for_card_image(self._page, _e, cw, ch, rail_w=_RAIL_W, meta_h=68)
            if u and rect:
                self._rect_by_uid[u] = rect

        badges: list[ft.Control] = []
        if dl:
            badges.append(
                ft.Container(
                    right=4,
                    top=4,
                    bgcolor=ft.Colors.with_opacity(0.92, "#166534"),
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=5, vertical=2),
                    content=ft.Text("DL", size=8, weight=ft.FontWeight.BOLD, color="#bbf7d0", no_wrap=True),
                )
            )
        if on_disk:
            badges.append(
                ft.Container(
                    right=4,
                    bottom=4,
                    bgcolor=ft.Colors.with_opacity(0.92, "#0f766e"),
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=5, vertical=2),
                    content=ft.Text("DISK", size=8, weight=ft.FontWeight.BOLD, color="#ccfbf1", no_wrap=True),
                )
            )
        return ft.Container(
            width=card_w,
            height=thumb_h,
            bgcolor="#111827",
            border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.GestureDetector(
                content=ft.Stack(
                    [
                        ft.Container(content=thumb_box, width=card_w, height=thumb_h),
                        bottom_text,
                        left_rail,
                        *badges,
                    ],
                    height=thumb_h,
                ),
                hover_interval=40,
                on_enter=_on_enter,
                on_hover=_on_hover,
            ),
        )

    def _preview_one(self, uid: str, name: str, viewer_url: str) -> None:
        if uid and self._on_preview:
            r = self._rect_by_uid.get(uid)
            try:
                self._on_preview(uid, name, viewer_url, "", rect=r)
            except TypeError:
                self._on_preview(uid, name, viewer_url)

    def _download_one(self, uid: str, name: str) -> None:
        if uid and self._on_download:
            self._on_download(uid, name)

    def _toggle_like(self, uid: str) -> None:
        uid = _norm_uid(uid)
        if not uid or not self._on_like:
            return
        self._interaction_block_until = time.monotonic() + 1.4
        self._on_like(uid, unlike=(uid in self._liked_uids))

    def search_params(self) -> dict:
        from browse_dates import api_date_days

        self._date_filter_key = self._date_dd.value or ""
        cat = self._category_dd.value or ""
        sort = self._sort_dd.value or "-viewCount"
        # API has no real downloadCount sort — fetch popular downloadable, re-sort after enrich.
        if sort == _LOCAL_DL_SORT:
            sort = "-likeCount"
            if not self._dl_only.value:
                self._dl_only.value = True
        min_dl = self._min_downloads()
        if min_dl > 0 and not self._dl_only.value:
            self._dl_only.value = True
        params = {
            "query": self._query.value or "",
            "sort_by": sort,
            "downloadable": bool(self._dl_only.value) or None,
            "staffpicked": bool(self._staff_only.value) or None,
            "animated": bool(self._anim_only.value) or None,
            "categories": cat if cat else None,
            "date": api_date_days(self._date_filter_key),
        }
        self._last_search_params = dict(params)
        return params
