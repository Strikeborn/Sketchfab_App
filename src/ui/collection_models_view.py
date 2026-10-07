"""Shared UI: open a collection and browse its models as cards."""
from __future__ import annotations

import threading
import time
from typing import Callable

import flet as ft

from browse_collections import unwrap_collection_model
from browse_models import normalize_search_model
from content_filter import filter_rows
from model_resolve import author_matches
import perf_log
from ui.liked_style import download_icon_style, row_is_downloadable
from ui.scroll_drag import wrap_middle_drag_scroll
from ui.thumb_lod import make_lod_thumb, ViewportLod, ScrollLodDebouncer

_MUTED = "#94a3b8"
_TEXT = "#e2e8f0"
_CARD_BG = "#1e293b"
_ACCENT = "#38bdf8"
_GRID_GAP = 8
_MIN_COLS = 3
_MAX_COLS = 8
_SIDE_CHROME = 360
_PAGE_SIZE = 24
_SCROLL_LOAD_THRESHOLD = 180
_SKELETON_CARDS = 8
_PROGRESSIVE_ROWS = 2  # paint this many grid rows first, then append
_CACHE_MAX = 10
_CACHE_TTL_SEC = 600.0

# uid|sort|dl → {t, rows, next, meta}
_OPEN_CACHE: dict[str, dict] = {}
_OPEN_CACHE_ORDER: list[str] = []

_SORT_OPTS = [
    ("order", "Collection order"),
    ("-viewCount", "Most views"),
    ("-likeCount", "Most likes"),
]

_LIKED_FILTER_OPTS = [
    ("all", "All"),
    ("hide", "Hide liked"),
    ("only", "Liked only"),
]


def _fmt_count(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


def _row_matches_author(row: dict, author: str) -> bool:
    author = (author or "").strip()
    if not author:
        return True
    fake = {
        "user": {
            "username": str(row.get("Author Username") or row.get("Author") or ""),
            "displayName": str(row.get("Author") or ""),
        }
    }
    return author_matches(fake, author)


def _cache_key(uid: str, sort_by: str, downloadable: bool) -> str:
    return f"{uid}|{sort_by or 'order'}|{int(bool(downloadable))}"


def _cache_get(key: str) -> dict | None:
    ent = _OPEN_CACHE.get(key)
    if not ent:
        return None
    if (time.time() - float(ent.get("t") or 0)) > _CACHE_TTL_SEC:
        _OPEN_CACHE.pop(key, None)
        try:
            _OPEN_CACHE_ORDER.remove(key)
        except ValueError:
            pass
        return None
    return ent


def _cache_put(key: str, *, rows: list[dict], next_url: str | None, meta: dict) -> None:
    _OPEN_CACHE[key] = {
        "t": time.time(),
        "rows": list(rows),
        "next": next_url,
        "meta": dict(meta or {}),
    }
    if key in _OPEN_CACHE_ORDER:
        _OPEN_CACHE_ORDER.remove(key)
    _OPEN_CACHE_ORDER.append(key)
    while len(_OPEN_CACHE_ORDER) > _CACHE_MAX:
        old = _OPEN_CACHE_ORDER.pop(0)
        _OPEN_CACHE.pop(old, None)


def invalidate_open_cache(uid: str | None = None) -> None:
    """Drop cached pages for one collection (or all)."""
    if not uid:
        _OPEN_CACHE.clear()
        _OPEN_CACHE_ORDER.clear()
        return
    prefix = f"{uid}|"
    dead = [k for k in list(_OPEN_CACHE) if k.startswith(prefix)]
    for k in dead:
        _OPEN_CACHE.pop(k, None)
        try:
            _OPEN_CACHE_ORDER.remove(k)
        except ValueError:
            pass


class CollectionModelsView(ft.Column):
    """Back header + model card grid for one collection."""

    def __init__(
        self,
        *,
        on_back: Callable[[], None] | None = None,
        on_preview: Callable[[str], None] | None = None,
        on_download: Callable[[str, str], None] | None = None,
        on_subscribe: Callable[[dict, bool], None] | None = None,
        on_remove: Callable[[str, str, dict], None] | None = None,
        on_like: Callable[[str], None] | None = None,
        on_open_account: Callable[[str], None] | None = None,
        get_client: Callable | None = None,
        allow_remove: bool = False,
    ):
        super().__init__(expand=True, spacing=6)
        self._on_back = on_back
        self._on_preview = on_preview
        self._on_download = on_download
        self._on_subscribe = on_subscribe
        self._on_remove = on_remove
        self._on_like = on_like
        self._on_open_account = on_open_account
        self._get_client = get_client
        self._allow_remove = bool(allow_remove)
        self._page: ft.Page | None = None
        self._meta: dict = {}
        self._rows: list[dict] = []
        self._next_url: str | None = None
        self._loading = False
        self._auto_loading = False
        self._removing: set[str] = set()
        self._subscribed_uids: set[str] = set()
        self._liked_uids: set[str] = set()
        self._like_btns: dict[str, ft.IconButton] = {}
        self._grid_cols = 5
        self._lod = ViewportLod()
        self._scroll_offset = 0.0
        self._viewport_h = 800.0
        self._load_gen = 0
        self._hide_nsfw = True
        self._hide_female = True
        self._hide_male = True
        self._pending_grid_tail: list[ft.Control] | None = None
        self._info_fn: Callable[[str], None] | None = None

        self._title = ft.Text("", size=14, weight=ft.FontWeight.BOLD, color=_TEXT)
        self._subtitle = ft.Row(spacing=4, tight=True, wrap=True)
        self._status = ft.Text("", size=11, color=_MUTED)
        self._count_label = ft.Text("", size=11, color=_MUTED)
        self._sub_btn = ft.OutlinedButton(
            "Subscribe",
            icon=ft.Icons.NOTIFICATIONS_ACTIVE,
            height=34,
            on_click=lambda e: self._toggle_subscribe(),
        )
        self._open_web = ft.IconButton(icon=ft.Icons.OPEN_IN_NEW, tooltip="Open on Sketchfab")
        self._load_more = ft.TextButton("Load more", on_click=lambda e: self._load_more_models())
        self._sort = ft.Dropdown(
            label="Sort",
            width=140,
            dense=True,
            text_size=12,
            value="order",
            options=[ft.dropdown.Option(v, t) for v, t in _SORT_OPTS],
            content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
            on_change=lambda e: self._on_query_change(api=True),
        )
        self._liked_filter = ft.Dropdown(
            label="Liked",
            width=120,
            dense=True,
            text_size=12,
            value="all",
            options=[ft.dropdown.Option(v, t) for v, t in _LIKED_FILTER_OPTS],
            content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
            on_change=lambda e: self._on_query_change(api=False),
        )
        self._dl_only = ft.Checkbox(
            label="DL only",
            value=False,
            tooltip="Downloadable only (Sketchfab filter)",
            on_change=lambda e: self._on_query_change(api=True),
        )
        self._author_filter = ft.TextField(
            label="Author",
            hint_text="Author name",
            width=120,
            dense=True,
            text_size=12,
            tooltip="Show only this author — loads remaining collection pages automatically",
            on_submit=lambda e: self._on_author_filter_apply(),
        )
        self._cols_slider = ft.Slider(
            min=float(_MIN_COLS),
            max=float(_MAX_COLS),
            divisions=_MAX_COLS - _MIN_COLS,
            value=float(self._grid_cols),
            width=120,
            height=28,
            label="{value}/row",
            on_change=self._on_cols,
        )
        self.scroller = ft.ListView(expand=True, spacing=4, padding=4, on_scroll_interval=40)
        self._scroller_wrap = wrap_middle_drag_scroll(
            self.scroller,
            expand=True,
            on_scroll=self._on_scroll,
        )
        self._lod_debouncer = ScrollLodDebouncer(self._lod, self, wait_s=0.05, throttle_s=0.045)
        self._ready = False

        self.controls = [
            ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        tooltip="Back to collections (Esc)",
                        on_click=lambda e: self._back(),
                    ),
                    ft.TextButton(
                        "← Collections",
                        tooltip="Back to Browse Collections search",
                        on_click=lambda e: self._back(),
                    ),
                    ft.Column([self._title, self._subtitle], spacing=0, expand=True, tight=True),
                    self._sub_btn,
                    self._open_web,
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Row(
                [
                    self._sort,
                    self._liked_filter,
                    self._dl_only,
                    self._author_filter,
                    ft.Text("Cols", size=11, color=_MUTED),
                    self._cols_slider,
                    ft.Container(expand=True),
                    self._count_label,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            self._scroller_wrap,
            ft.Row([self._load_more, ft.Container(expand=True)], spacing=4),
        ]

    def set_page(self, page: ft.Page) -> None:
        self._page = page

    def set_info_fn(self, fn: Callable[[str], None] | None) -> None:
        """Optional Activity logger for perf_log spans."""
        self._info_fn = fn

    def set_like_callback(self, cb) -> None:
        self._on_like = cb

    def set_open_account_callback(self, cb) -> None:
        self._on_open_account = cb

    def scroll_to_top(self) -> None:
        self._scroll_offset = 0.0
        try:
            self.scroller.scroll_to(offset=0, duration=200)
        except Exception:
            pass

    def restore_scroll(self) -> None:
        off = self._scroll_offset
        if off <= 0:
            return
        self.scroller.opacity = 0
        try:
            self.scroller.update()
        except Exception:
            pass

        async def _restore():
            try:
                self.scroller.scroll_to(offset=off, duration=0)
            except Exception:
                pass
            self.scroller.opacity = 1
            try:
                self.scroller.update()
            except Exception:
                pass
            if self._lod_debouncer and self._page:
                self._lod_debouncer.kick(
                    self._page,
                    scroll=off,
                    viewport=self._viewport_h,
                    remount_delay=0.05,
                )

        if self._page:
            self._page.run_task(_restore)
        else:
            self.scroller.opacity = 1

    def set_liked_uids(self, uids: set[str]) -> None:
        self._liked_uids = set(uids or set())
        if self._ready and self._rows and getattr(self, "page", None):
            try:
                self._apply_view(update=True)
            except Exception:
                pass

    def mark_liked(self, uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid:
            return
        self._liked_uids.add(uid)
        btn = self._like_btns.get(uid)
        if btn is not None:
            btn.icon = ft.Icons.FAVORITE
            btn.icon_color = "#f9a8d4"
            btn.tooltip = "Liked"
            btn.disabled = True
            try:
                if getattr(btn, "page", None):
                    btn.update()
            except Exception:
                pass
        # If filtering hide/only liked, refresh the grid.
        if (self._liked_filter.value or "all") != "all":
            self._apply_view(update=True)

    def set_subscribed_uids(self, uids: set[str]) -> None:
        self._subscribed_uids = set(uids or set())
        self._refresh_sub_btn()

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        self._hide_nsfw = bool(hide_nsfw)
        self._hide_female = bool(hide_female)
        self._hide_male = bool(hide_male)
        if self._ready and self._rows:
            self._apply_view(update=True)

    def _skeleton_controls(self) -> list[ft.Control]:
        """Placeholder cards so Open doesn't look like an empty broken pane."""
        card_w, thumb_h = self._card_dims()
        cols_n = max(_MIN_COLS, min(_MAX_COLS, self._grid_cols))
        cards = []
        for _ in range(_SKELETON_CARDS):
            cards.append(
                ft.Container(
                    width=card_w,
                    height=thumb_h,
                    bgcolor="#0b1220",
                    border_radius=8,
                    border=ft.border.all(1, "#1e293b"),
                    content=ft.Column(
                        [
                            ft.Container(expand=True),
                            ft.Container(
                                height=28,
                                bgcolor="#111827",
                                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                                content=ft.Container(height=10, width=card_w // 2, bgcolor="#1e293b", border_radius=4),
                            ),
                        ],
                        spacing=0,
                    ),
                )
            )
        rows: list[ft.Control] = [
            ft.Container(
                padding=ft.padding.only(left=8, top=4, bottom=4),
                content=ft.Row(
                    [
                        ft.ProgressRing(width=16, height=16, stroke_width=2),
                        ft.Text("Loading models…", size=12, color=_MUTED),
                    ],
                    spacing=8,
                    tight=True,
                ),
            )
        ]
        for i in range(0, len(cards), cols_n):
            rows.append(
                ft.Container(
                    padding=ft.padding.only(bottom=6),
                    content=ft.Row(cards[i : i + cols_n], spacing=_GRID_GAP, wrap=False),
                )
            )
        return rows

    def cancel_pending(self) -> None:
        """Invalidate in-flight collection model fetches (app shutdown)."""
        self._load_gen += 1
        self._loading = False
        self._auto_loading = False

    def open_collection(self, meta: dict, *, fetch: bool = True, author_filter: str = "") -> None:
        self._meta = dict(meta or {})
        self._rows = []
        self._next_url = None
        self._loading = False
        self._auto_loading = False
        self._ready = False
        self._pending_grid_tail = None
        self._like_btns.clear()
        self._load_gen += 1
        self._author_filter.value = (author_filter or "").strip()
        name = self._meta.get("Name") or self._meta.get("UID") or "Collection"
        self._title.value = name
        self._paint_subtitle()
        url = self._meta.get("URL") or ""
        self._open_web.url = url or None
        self._open_web.disabled = not bool(url)
        self._refresh_sub_btn()

        uid = str(self._meta.get("UID") or "")
        sort_by = self._api_sort_by()
        downloadable = bool(self._dl_only.value)
        key = _cache_key(uid, sort_by, downloadable) if uid else ""
        cached = _cache_get(key) if key else None
        if cached and fetch:
            self._rows = list(cached.get("rows") or [])
            self._next_url = cached.get("next")
            if cached.get("meta"):
                self._meta = {**self._meta, **cached["meta"]}
                self._title.value = self._meta.get("Name") or name
                self._paint_subtitle()
            self._load_more.visible = True
            self._load_more.disabled = not bool(self._next_url)
            self._ready = True
            self._apply_view(update=False, progressive=True)
            self._count_label.value = self._status_counts(len(self._display_rows()))
            try:
                self.update()
            except Exception:
                pass
            self._schedule_progressive_flush()
            if self._info_fn:
                try:
                    self._info_fn("Open collection: cache hit")
                except Exception:
                    pass
            self._maybe_fetch_all_for_author()
            return

        self.scroller.controls = self._skeleton_controls()
        self._load_more.visible = True
        self._load_more.disabled = True
        self._count_label.value = "Loading…"
        try:
            self.update()
        except Exception:
            pass
        if fetch:
            gen = self._load_gen
            threading.Thread(target=self._fetch_first_page, args=(gen,), daemon=True).start()

    def open_model_list(self, meta: dict, rows: list[dict], *, author_filter: str = "") -> None:
        """Show pre-built model rows (author scan, export, etc.) — no API fetch."""
        self._meta = dict(meta or {})
        self._rows = list(rows or [])
        self._next_url = None
        self._loading = False
        self._auto_loading = False
        self._ready = True
        self._pending_grid_tail = None
        self._like_btns.clear()
        self._load_gen += 1
        self._author_filter.value = (author_filter or "").strip()
        name = self._meta.get("Name") or "Results"
        self._title.value = name
        self._paint_subtitle()
        url = self._meta.get("URL") or ""
        self._open_web.url = url or None
        self._open_web.disabled = not bool(url)
        self._refresh_sub_btn()
        self._load_more.visible = False
        self._load_more.disabled = True
        self._apply_view(update=False, progressive=True)
        self._count_label.value = self._status_counts(len(self._display_rows()))
        try:
            self.update()
        except Exception:
            pass
        self._schedule_progressive_flush()

    def _on_author_filter_apply(self) -> None:
        self._apply_view()
        self._maybe_fetch_all_for_author()

    def _maybe_fetch_all_for_author(self) -> None:
        if not (self._author_filter.value or "").strip():
            return
        if self._next_url and not self._auto_loading and not self._loading:
            self._fetch_all_remaining_for_author()

    def _fetch_all_remaining_for_author(self) -> None:
        if self._auto_loading or not self._next_url:
            return
        uid = str(self._meta.get("UID") or "")
        if not uid:
            return
        self._auto_loading = True
        gen = self._load_gen
        author = (self._author_filter.value or "").strip()
        if self._info_fn:
            try:
                self._info_fn(f"Loading full collection to filter by {author}…")
            except Exception:
                pass

        def work():
            err = None
            batch: list[dict] = []
            next_url = self._next_url
            while next_url and gen == self._load_gen:
                try:
                    data = self._client().list_collection_models(uid, cursor_url=next_url)
                    batch.extend(self._append_page_rows(data))
                    next_url = data.get("next")
                except Exception as exc:
                    err = str(exc)
                    break

            def apply():
                if gen != self._load_gen:
                    return
                self._auto_loading = False
                if err:
                    self._count_label.value = f"Load failed: {err}"
                else:
                    seen = {r.get("UID") for r in self._rows}
                    for r in batch:
                        u = r.get("UID")
                        if u and u not in seen:
                            self._rows.append(r)
                            seen.add(u)
                    self._next_url = next_url
                    self._load_more.disabled = not bool(next_url)
                    self._apply_view()
                    if self._info_fn:
                        shown = len(self._display_rows())
                        try:
                            self._info_fn(
                                f"Author “{author}”: {shown} model(s) in collection ({len(self._rows)} loaded)"
                            )
                        except Exception:
                            pass
                try:
                    self.update()
                except Exception:
                    pass

            self._ui(apply)

        threading.Thread(target=work, daemon=True).start()

    def _api_sort_by(self) -> str:
        v = (self._sort.value or "order").strip()
        return "" if v in {"order", "default", ""} else v

    def _on_query_change(self, *, api: bool) -> None:
        """Sort/DL → Sketchfab re-fetch from page 1. Liked → local filter."""
        if not self._meta.get("UID"):
            return
        if api:
            self._load_gen += 1
            self._rows = []
            self._next_url = None
            self._ready = False
            self.scroller.controls = self._skeleton_controls()
            self._load_more.disabled = True
            self._count_label.value = "Loading…"
            try:
                self.update()
            except Exception:
                pass
            gen = self._load_gen
            threading.Thread(target=self._fetch_first_page, args=(gen,), daemon=True).start()
            return
        if not self._ready:
            return
        self._apply_view(update=True)

    def _paint_subtitle(self, *, extra: str = "") -> None:
        author = str(self._meta.get("Author") or "").strip()
        author_key = str(self._meta.get("Author Username") or author or "").strip()
        models = _fmt_count(self._meta.get("Model Count"))
        subs = _fmt_count(self._meta.get("Subscriber Count"))
        bits: list[ft.Control] = []
        if author_key and self._on_open_account:
            bits.append(
                ft.TextButton(
                    text=author or author_key,
                    style=ft.ButtonStyle(padding=0, color=_ACCENT),
                    tooltip=f"Open @{author_key} in Account",
                    on_click=lambda e, k=author_key: self._on_open_account(k) if self._on_open_account else None,
                )
            )
        elif author:
            bits.append(ft.Text(author, size=11, color=_MUTED))
        for label in (f"{models} models", f"{subs} subscribers"):
            if label and not label.startswith("—"):
                if bits:
                    bits.append(ft.Text("·", size=11, color=_MUTED))
                bits.append(ft.Text(label, size=11, color=_MUTED))
        if extra:
            if bits:
                bits.append(ft.Text("·", size=11, color=_MUTED))
            bits.append(ft.Text(extra, size=11, color=_MUTED))
        self._subtitle.controls = bits

    def _ui(self, fn) -> None:
        async def _run():
            fn()

        if self._page:
            try:
                self._page.run_task(_run)
                return
            except Exception:
                pass
        fn()

    def _back(self) -> None:
        self.cancel_pending_loads()
        if self._on_back:
            self._on_back()

    def cancel_pending_loads(self) -> None:
        """Stop in-flight collection page fetches when navigating away."""
        self._load_gen += 1
        self._loading = False
        self._auto_loading = False

    def _refresh_sub_btn(self) -> None:
        uid = str(self._meta.get("UID") or "")
        on = uid in self._subscribed_uids
        self._sub_btn.text = "Unsubscribe" if on else "Subscribe"
        self._sub_btn.icon = ft.Icons.NOTIFICATIONS_OFF if on else ft.Icons.NOTIFICATIONS_ACTIVE
        self._sub_btn.visible = bool(uid) and self._on_subscribe is not None

    def _toggle_subscribe(self) -> None:
        if not self._on_subscribe or not self._meta.get("UID"):
            return
        uid = str(self._meta["UID"])
        want = uid not in self._subscribed_uids
        self._sub_btn.disabled = True
        self.update()

        def work():
            err = None
            try:
                self._on_subscribe(self._meta, want)
            except Exception as e:
                err = str(e)

            def done():
                self._sub_btn.disabled = False
                if err:
                    self._status.value = f"Subscribe failed: {err}"
                else:
                    if want:
                        self._subscribed_uids.add(uid)
                    else:
                        self._subscribed_uids.discard(uid)
                    self._refresh_sub_btn()
                    msg = "Subscribed." if want else "Unsubscribed."
                    if self._info_fn:
                        try:
                            self._info_fn(msg)
                        except Exception:
                            pass
                try:
                    self.update()
                except Exception:
                    pass

            self._ui(done)

        threading.Thread(target=work, daemon=True).start()

    def _on_cols(self, e) -> None:
        try:
            self._grid_cols = int(float(e.control.value))
        except (TypeError, ValueError):
            return
        self._apply_view(update=True)

    def _display_rows(self) -> list[dict]:
        """Liked + N/W/M + author are local; sort/DL already applied by Sketchfab when fetching."""
        rows = list(self._rows)
        author = (self._author_filter.value or "").strip()
        if author:
            rows = [r for r in rows if _row_matches_author(r, author)]
        liked_mode = self._liked_filter.value or "all"
        if liked_mode == "hide" and self._liked_uids:
            rows = [r for r in rows if str(r.get("UID") or "") not in self._liked_uids]
        elif liked_mode == "only":
            rows = [r for r in rows if str(r.get("UID") or "") in self._liked_uids]
        rows = filter_rows(
            rows,
            hide_nsfw=self._hide_nsfw,
            hide_female=self._hide_female,
            hide_male=self._hide_male,
        )
        return rows

    def _status_counts(self, shown: int) -> str:
        total_hint = self._meta.get("Model Count")
        loaded = len(self._rows)
        author = (self._author_filter.value or "").strip()
        if shown != loaded:
            count_bit = f"{shown:,} shown · {loaded:,} loaded"
        else:
            count_bit = f"{loaded:,} loaded"
            if total_hint is not None:
                try:
                    count_bit = f"{loaded:,} / {int(total_hint):,}"
                except (TypeError, ValueError):
                    pass
        if author:
            count_bit = f"{count_bit} · author={author}"
        if self._auto_loading:
            count_bit += " · loading…"
        more = " · scroll for more" if self._next_url and not self._auto_loading else ""
        return f"{count_bit}{more}"

    def _apply_view(
        self,
        *,
        update: bool = True,
        keep_scroll: float | None = None,
        progressive: bool = False,
    ) -> None:
        self._render(progressive=progressive)
        shown = len(self._display_rows())
        self._count_label.value = self._status_counts(shown)
        if update:
            try:
                self.update()
            except Exception:
                pass
        if keep_scroll is not None and keep_scroll > 0:
            try:
                self.scroller.scroll_to(offset=keep_scroll, duration=0)
            except Exception:
                pass
        if update and progressive:
            self._schedule_progressive_flush()
        elif update and self._lod_debouncer and self._page:
            self._lod_debouncer.kick(
                self._page,
                scroll=keep_scroll if keep_scroll is not None else self._scroll_offset,
                viewport=self._viewport_h,
                remount_delay=0.04,
            )

    def _schedule_progressive_flush(self) -> None:
        if not self._pending_grid_tail or not self._page:
            if self._lod_debouncer and self._page:
                self._lod_debouncer.kick(
                    self._page,
                    scroll=self._scroll_offset,
                    viewport=self._viewport_h,
                    remount_delay=0.12,
                )
            return

        async def _flush():
            import asyncio

            await asyncio.sleep(0.02)
            tail = self._pending_grid_tail
            self._pending_grid_tail = None
            if not tail:
                return
            try:
                self.scroller.controls = list(self.scroller.controls or []) + list(tail)
                self.update()
            except Exception:
                pass
            if self._lod_debouncer and self._page:
                self._lod_debouncer.kick(
                    self._page,
                    scroll=self._scroll_offset,
                    viewport=self._viewport_h,
                    remount_delay=0.12,
                )

        try:
            self._page.run_task(_flush)
        except Exception:
            # Fallback: append sync if run_task unavailable
            try:
                self.scroller.controls = list(self.scroller.controls or []) + list(self._pending_grid_tail or [])
                self._pending_grid_tail = None
            except Exception:
                pass

    def _like_one(self, uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid or uid in self._liked_uids:
            return
        if not self._ready:
            return
        self.mark_liked(uid)
        if self._on_like:
            self._on_like(uid)

    def _fetch_first_page(self, gen: int) -> None:
        """Load first Sketchfab page (with sort/DL) and show the grid.

        Skips get_collection — list-row meta (name/count/thumb/url) is enough for chrome.
        """
        uid = str(self._meta.get("UID") or "")
        if not uid:
            return
        sort_by = self._api_sort_by()
        downloadable = bool(self._dl_only.value)
        self._loading = True
        err = None
        rows: list[dict] = []
        next_url = None
        meta = dict(self._meta)
        t0 = time.perf_counter()
        try:
            client = self._client()
            data = client.list_collection_models(
                uid,
                count=_PAGE_SIZE,
                sort_by=sort_by,
                downloadable=downloadable or None,
            )
            rows = self._append_page_rows(data)
            next_url = data.get("next")
        except Exception as e:
            err = str(e)
        net_ms = (time.perf_counter() - t0) * 1000.0

        if gen != self._load_gen:
            return

        def apply():
            if gen != self._load_gen:
                return
            self._loading = False
            self._meta = meta
            self._title.value = meta.get("Name") or uid
            self._paint_subtitle()
            url = meta.get("URL") or ""
            self._open_web.url = url or None
            self._open_web.disabled = not bool(url)
            self._refresh_sub_btn()
            if err:
                self.scroller.controls = [
                    ft.Container(
                        padding=20,
                        content=ft.Text(f"Failed to load: {err}", size=13, color="#f87171"),
                    )
                ]
                self._count_label.value = ""
                self._load_more.disabled = True
                self._ready = False
            else:
                self._rows = rows
                self._next_url = next_url
                self._load_more.disabled = not bool(next_url)
                self._ready = True
                _cache_put(
                    _cache_key(uid, sort_by, downloadable),
                    rows=rows,
                    next_url=next_url,
                    meta=meta,
                )
                paint_t0 = time.perf_counter()
                self._apply_view(update=False, progressive=True)
                paint_ms = (time.perf_counter() - paint_t0) * 1000.0
                perf_log.mark(
                    f"Open collection net {net_ms:.0f}ms · first paint {paint_ms:.0f}ms ({len(rows)} models)",
                    net_ms + paint_ms,
                    info=self._info_fn,
                )
            try:
                self.update()
            except Exception:
                pass
            if not err:
                self._schedule_progressive_flush()
                self._maybe_fetch_all_for_author()

        self._ui(apply)

    def _on_scroll(self, e: ft.OnScrollEvent) -> None:
        try:
            self._scroll_offset = float(e.pixels or 0)
            self._viewport_h = float(e.viewport_dimension or self._viewport_h or 800)
        except (TypeError, ValueError):
            pass
        if self._lod_debouncer and self._page:
            self._lod_debouncer.on_scroll(e, self._page)
        if self._loading or not self._next_url:
            return
        try:
            near_bottom = (self._scroll_offset + self._viewport_h) >= (
                float(e.max_scroll_extent or 0) - _SCROLL_LOAD_THRESHOLD
            )
        except (TypeError, ValueError):
            near_bottom = False
        if near_bottom:
            self._load_more_models()

    def _client(self):
        if not self._get_client:
            raise RuntimeError("No Sketchfab client")
        return self._get_client()

    def _append_page_rows(self, data: dict) -> list[dict]:
        rows: list[dict] = []
        for item in data.get("results") or []:
            model = unwrap_collection_model(item)
            if model.get("uid"):
                rows.append(normalize_search_model(model))
        return rows

    def _load_more_models(self) -> None:
        if self._loading or not self._next_url:
            return
        self._loading = True
        self._load_more.disabled = True
        try:
            self._load_more.update()
        except Exception:
            pass
        next_url = self._next_url
        gen = self._load_gen
        keep_scroll = self._scroll_offset

        def work():
            err = None
            rows: list[dict] = []
            new_next = None
            try:
                data = self._client().list_collection_models(
                    str(self._meta.get("UID") or ""),
                    cursor_url=next_url,
                )
                rows = self._append_page_rows(data)
                new_next = data.get("next")
            except Exception as e:
                err = str(e)

            def apply():
                if gen != self._load_gen:
                    return
                self._loading = False
                if err:
                    self._count_label.value = f"Load more failed: {err}"
                    self._load_more.disabled = False
                else:
                    seen = {r.get("UID") for r in self._rows}
                    for r in rows:
                        if r.get("UID") not in seen:
                            self._rows.append(r)
                            seen.add(r.get("UID"))
                    self._next_url = new_next
                    self._load_more.disabled = not bool(new_next)
                    self._apply_view(update=False, keep_scroll=keep_scroll)
                self.update()
                if keep_scroll and keep_scroll > 0:
                    try:
                        self.scroller.scroll_to(offset=keep_scroll, duration=0)
                    except Exception:
                        pass
                if self._lod_debouncer and self._page and not err:
                    self._lod_debouncer.kick(
                        self._page,
                        scroll=keep_scroll,
                        viewport=self._viewport_h,
                        remount_delay=0.06,
                    )

            self._ui(apply)

        threading.Thread(target=work, daemon=True).start()

    def _card_dims(self) -> tuple[int, int]:
        page_w = int(self._page.width or 1200) if self._page else 1200
        avail = max(400, page_w - _SIDE_CHROME)
        cols = max(_MIN_COLS, min(_MAX_COLS, self._grid_cols))
        card_w = max(96, int((avail - _GRID_GAP * (cols - 1)) / cols))
        return card_w, max(72, card_w - 4)

    def _render(self, *, progressive: bool = False) -> None:
        self._lod.clear()
        self._like_btns.clear()
        self._pending_grid_tail = None
        rows = self._display_rows()
        if not rows:
            msg = "No models in this collection."
            if self._rows:
                msg = "No models match the current sort/filters."
            self.scroller.controls = [
                ft.Container(
                    padding=20,
                    content=ft.Text(msg, size=13, color=_MUTED),
                )
            ]
            return
        card_w, thumb_h = self._card_dims()
        cols_n = max(_MIN_COLS, min(_MAX_COLS, self._grid_cols))
        card_h = float(thumb_h + 52)
        gap = float(_GRID_GAP)
        cards = []
        for i, r in enumerate(rows):
            row_i = i // cols_n
            y0 = row_i * (card_h + gap)
            cards.append(self._model_card(r, card_w, thumb_h, lod_y0=y0, lod_y1=y0 + card_h))
        grid_rows: list[ft.Control] = []
        for i in range(0, len(cards), cols_n):
            grid_rows.append(
                ft.Container(
                    padding=ft.padding.only(bottom=6),
                    content=ft.Row(cards[i : i + cols_n], spacing=_GRID_GAP, wrap=False),
                )
            )
        if progressive and len(grid_rows) > _PROGRESSIVE_ROWS:
            self.scroller.controls = grid_rows[:_PROGRESSIVE_ROWS]
            self._pending_grid_tail = grid_rows[_PROGRESSIVE_ROWS:]
        else:
            self.scroller.controls = grid_rows

    def _model_card(
        self,
        row: dict,
        card_w: int,
        thumb_h: int,
        *,
        lod_y0: float,
        lod_y1: float,
    ) -> ft.Container:
        low = row.get("Thumbnail") or ""
        hi = row.get("Thumbnail HD") or low
        box, img = make_lod_thumb(low or hi, hi or low, size=thumb_h)
        if img is not None:
            img.width = card_w
            img.height = thumb_h
            box.width = card_w
            box.height = thumb_h
            self._lod.add(img, low or hi, hi or low, lod_y0, lod_y1)
        uid = str(row.get("UID") or "")
        name = str(row.get("Name") or uid)
        author = str(row.get("Author") or "")
        author_key = str(row.get("Author Username") or author or "").strip()
        likes = int(row.get("likeCount") or 0)
        views = int(row.get("viewCount") or 0)
        lic = str(row.get("License") or "")
        liked = uid in self._liked_uids
        dl = row_is_downloadable(row.get("Downloadable"))
        dl_color, dl_tip = download_icon_style(dl)

        def preview(_e=None, u=uid):
            if self._on_preview and u:
                self._on_preview(u)

        def download(_e=None, u=uid, n=name):
            if self._on_download and u:
                self._on_download(u, n)

        def remove(_e=None, u=uid):
            self._remove_model(u)

        def like(_e=None, u=uid):
            self._like_one(u)

        def open_author(_e=None, k=author_key):
            if k and self._on_open_account:
                self._on_open_account(k)

        like_btn = ft.IconButton(
            icon=ft.Icons.FAVORITE if liked else ft.Icons.FAVORITE_BORDER,
            icon_size=16,
            icon_color="#f9a8d4" if liked else "#ffffff",
            tooltip="Liked" if liked else "Like",
            disabled=liked or not uid or self._on_like is None,
            on_click=like,
        )
        if uid:
            self._like_btns[uid] = like_btn

        thumb_stack: list[ft.Control] = [ft.GestureDetector(content=box, on_tap=preview)]
        if self._allow_remove and uid:
            thumb_stack.append(
                ft.Container(
                    right=2,
                    top=2,
                    content=ft.IconButton(
                        icon=ft.Icons.CLOSE,
                        icon_size=14,
                        icon_color="#f8fafc",
                        bgcolor=ft.Colors.with_opacity(0.72, "#0f172a"),
                        tooltip="Remove from this collection",
                        style=ft.ButtonStyle(padding=2),
                        width=28,
                        height=28,
                        on_click=remove,
                        disabled=uid in self._removing,
                    ),
                )
            )
        # Like overlay top-left (or under remove on the right if remove present)
        thumb_stack.append(
            ft.Container(
                left=2,
                top=2,
                content=like_btn,
            )
        )

        return ft.Container(
            width=card_w,
            bgcolor=_CARD_BG,
            border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Column(
                [
                    ft.Stack(thumb_stack, width=card_w, height=thumb_h),
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=6, vertical=4),
                        content=ft.Column(
                            [
                                ft.TextButton(
                                    text=name,
                                    url=f"https://sketchfab.com/3d-models/{uid}" if uid else None,
                                    style=ft.ButtonStyle(padding=0, color=_TEXT),
                                    tooltip=name,
                                )
                                if uid
                                else ft.Text(
                                    name,
                                    size=11,
                                    weight=ft.FontWeight.W_600,
                                    color=_TEXT,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Row(
                                    [
                                        (
                                            ft.TextButton(
                                                text=author or author_key,
                                                style=ft.ButtonStyle(padding=0, color=_ACCENT),
                                                tooltip=f"Open @{author_key} in Account",
                                                on_click=open_author,
                                            )
                                            if author_key and self._on_open_account
                                            else ft.Text(author or "—", size=10, color=_MUTED)
                                        ),
                                        ft.Text(
                                            f" · {views:,} views · {likes:,} likes",
                                            size=10,
                                            color=_MUTED,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                            expand=True,
                                        ),
                                    ],
                                    spacing=0,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                ft.Row(
                                    [
                                        ft.Text(lic, size=10, color="#fef08a", expand=True, max_lines=1),
                                        ft.IconButton(
                                            icon=ft.Icons.DOWNLOAD,
                                            icon_size=16,
                                            icon_color=dl_color,
                                            tooltip=dl_tip,
                                            on_click=download,
                                        ),
                                    ],
                                    spacing=0,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                            ],
                            spacing=1,
                            tight=True,
                        ),
                    ),
                ],
                spacing=0,
                tight=True,
            ),
        )

    def _remove_model(self, model_uid: str) -> None:
        model_uid = str(model_uid or "").strip()
        coll_uid = str(self._meta.get("UID") or "").strip()
        if not model_uid or not coll_uid or model_uid in self._removing:
            return
        if not self._allow_remove:
            return
        self._removing.add(model_uid)
        self._status.value = "Removing…"
        self.update()

        def work():
            err = None
            try:
                if self._on_remove:
                    self._on_remove(model_uid, coll_uid, dict(self._meta))
                else:
                    self._client().remove_model_from_collection(coll_uid, model_uid)
            except Exception as e:
                err = str(e)

            def done():
                self._removing.discard(model_uid)
                if err:
                    self._status.value = f"Remove failed: {err}"
                else:
                    self._rows = [r for r in self._rows if str(r.get("UID") or "") != model_uid]
                    mc = self._meta.get("Model Count")
                    try:
                        if mc is not None:
                            self._meta["Model Count"] = max(0, int(mc) - 1)
                    except (TypeError, ValueError):
                        pass
                    self._paint_subtitle()
                    self._status.value = f"Removed · {self._status_counts(len(self._display_rows()))}"
                    invalidate_open_cache(coll_uid)
                    self._render()
                self.update()
                if self._lod_debouncer and self._page and not err:
                    self._lod_debouncer.kick(
                        self._page,
                        scroll=self._scroll_offset,
                        viewport=self._viewport_h,
                        remount_delay=0.05,
                    )

            self._ui(done)

        threading.Thread(target=work, daemon=True).start()
