"""Browse Collections — search other people's collections, open models, subscribe."""
from __future__ import annotations

import threading
from typing import Callable

import flet as ft

from browse_collections import normalize_collection
from content_filter import passes_content_filter
from ui.collection_models_view import CollectionModelsView
from ui.scroll_drag import wrap_middle_drag_scroll

_PANEL = "#0f172a"
_MUTED = "#94a3b8"
_TEXT = "#e2e8f0"
_CARD_BG = "#1e293b"
_ACCENT = "#38bdf8"

_SORT_OPTS = [
    ("-subscriberCount", "Most subscribed"),
    ("-modelCount", "Most models"),
    ("-createdAt", "Newest"),
    ("createdAt", "Oldest"),
]


def _fmt_count(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


class BrowseCollectionsTab(ft.Column):
    def __init__(self, page: ft.Page | None = None):
        super().__init__(expand=True, spacing=6)
        self._page = page
        self._get_client: Callable | None = None
        self._on_preview = None
        self._on_download = None
        self._on_subscribe = None
        self._on_subscribe_ui = None
        self._on_open_account = None
        self._on_author_scan = None
        self._subscribed_uids: set[str] = set()
        self._rows: list[dict] = []
        self._next_url: str | None = None
        self._loading = False
        self._scroll_offset = 0.0
        self._list_scroll_saved = 0.0
        self._hide_nsfw = True
        self._hide_female = True
        self._hide_male = True

        self._query = ft.TextField(
            label="Search collections",
            hint_text="Name keywords…",
            width=260,
            dense=True,
            text_size=12,
            on_submit=lambda e: self._search(),
        )
        self._user = ft.TextField(
            label="By user",
            hint_text="username",
            width=140,
            dense=True,
            text_size=12,
            on_submit=lambda e: self._search(),
        )
        self._author_scan = ft.TextField(
            label="Find author in collections",
            hint_text="Author — scans web + your subscriptions",
            width=220,
            dense=True,
            text_size=12,
            on_submit=lambda e: self._scan_author(),
        )
        self._sort = ft.Dropdown(
            label="Sort",
            width=160,
            dense=True,
            text_size=12,
            value="-subscriberCount",
            options=[ft.dropdown.Option(v, t) for v, t in _SORT_OPTS],
            content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
        )
        self._status = ft.Text("Search public collections on Sketchfab.", size=12, color=_MUTED)
        self._load_more = ft.TextButton("Load more", disabled=True, on_click=lambda e: self._load_more_page())

        self.scroller = ft.ListView(expand=True, spacing=8, padding=8, on_scroll_interval=40)
        self._scroller_wrap = wrap_middle_drag_scroll(
            self.scroller,
            expand=True,
            on_scroll=self._on_scroll,
        )

        self._list_panel = ft.Column(
            expand=True,
            spacing=6,
            controls=[
                ft.Container(
                    padding=10,
                    bgcolor=_PANEL,
                    border_radius=8,
                    content=ft.Column(
                        [
                            ft.Text("Browse collections", size=13, weight=ft.FontWeight.BOLD),
                            ft.Text(
                                "Search other people's collections, open them as model cards, and subscribe.",
                                size=11,
                                color=_MUTED,
                            ),
                            ft.Row(
                                [
                                    self._query,
                                    self._user,
                                    self._author_scan,
                                    self._sort,
                                    ft.ElevatedButton(
                                        "Search",
                                        icon=ft.Icons.TRAVEL_EXPLORE,
                                        height=36,
                                        on_click=lambda e: self._search(),
                                    ),
                                    ft.OutlinedButton(
                                        "Scan author",
                                        icon=ft.Icons.PERSON_SEARCH,
                                        height=36,
                                        tooltip="Search public collections for an author, open models by that author",
                                        on_click=lambda e: self._scan_author(),
                                    ),
                                    self._load_more,
                                    ft.Container(expand=True),
                                    self._status,
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
                ),
                self._scroller_wrap,
            ],
        )

        self._models_view = CollectionModelsView(
            on_back=self._close_models,
            on_preview=lambda uid: self._on_preview(uid) if self._on_preview else None,
            on_download=lambda uid, name: self._on_download(uid, name) if self._on_download else None,
            on_subscribe=self._subscribe_collection,
            get_client=lambda: self._get_client() if self._get_client else None,
        )
        self._models_view.visible = False
        if page:
            self._models_view.set_page(page)

        self.controls = [self._list_panel, self._models_view]
        self.scroller.controls = [
            ft.Container(
                padding=24,
                content=ft.Text("Enter a search or username, then click Search.", size=13, color=_MUTED),
            )
        ]

    def set_page(self, page: ft.Page) -> None:
        self._page = page
        self._models_view.set_page(page)

    def set_client_factory(self, fn) -> None:
        self._get_client = fn

    def set_info_fn(self, fn) -> None:
        try:
            self._models_view.set_info_fn(fn)
        except Exception:
            pass

    def set_model_callbacks(self, on_preview=None, on_download=None, on_open_account=None) -> None:
        self._on_preview = on_preview
        self._on_download = on_download
        self._on_open_account = on_open_account
        self._models_view.set_open_account_callback(on_open_account)

    def set_like_callback(self, cb) -> None:
        self._models_view.set_like_callback(cb)

    def set_liked_uids(self, uids: set[str]) -> None:
        self._models_view.set_liked_uids(uids)

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        self._hide_nsfw = bool(hide_nsfw)
        self._hide_female = bool(hide_female)
        self._hide_male = bool(hide_male)
        try:
            self._models_view.set_content_filters(hide_nsfw, hide_female, hide_male)
        except Exception:
            pass
        if self._rows and not getattr(self._models_view, "visible", False):
            self._render_cards()

    def mark_liked(self, uid: str) -> None:
        self._models_view.mark_liked(uid)

    def set_subscribe_callback(self, cb) -> None:
        self._on_subscribe = cb

    def set_subscribe_ui_hook(self, cb) -> None:
        self._on_subscribe_ui = cb

    def set_author_scan_callback(self, cb) -> None:
        self._on_author_scan = cb

    def set_subscribed_uids(self, uids: set[str]) -> None:
        self._subscribed_uids = set(uids or set())
        self._models_view.set_subscribed_uids(self._subscribed_uids)
        self._render_cards()

    def on_tab_shown(self) -> None:
        if getattr(self._models_view, "visible", False):
            self._models_view.restore_scroll()
            return
        off = self._scroll_offset
        if not self._rows or off <= 0:
            self.update()
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
                self.update()

        if self._page:
            self._page.run_task(_restore)
        else:
            self.scroller.opacity = 1
            self.update()

    def scroll_to_top(self) -> None:
        if getattr(self._models_view, "visible", False):
            self._models_view.scroll_to_top()
            return
        self._scroll_offset = 0.0
        try:
            self.scroller.scroll_to(offset=0, duration=200)
        except Exception:
            pass

    def _on_scroll(self, e: ft.OnScrollEvent) -> None:
        try:
            self._scroll_offset = float(e.pixels or 0)
        except Exception:
            pass

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

    def _client(self):
        if not self._get_client:
            raise RuntimeError("No Sketchfab client")
        return self._get_client()

    def _search(self) -> None:
        if self._loading:
            return
        self._loading = True
        self._status.value = "Searching…"
        self._load_more.disabled = True
        self.update()
        q = (self._query.value or "").strip()
        user = (self._user.value or "").strip()
        sort_by = self._sort.value or "-subscriberCount"

        def work():
            err = None
            rows: list[dict] = []
            next_url = None
            try:
                data = self._client().search_collections(query=q, user=user, sort_by=sort_by, count=24)
                for item in data.get("results") or []:
                    rows.append(normalize_collection(item))
                next_url = data.get("next")
            except Exception as e:
                err = str(e)

            def apply():
                self._loading = False
                if err:
                    self._status.value = f"Search failed: {err}"
                    self.scroller.controls = [
                        ft.Container(padding=20, content=ft.Text(err, size=13, color="#f87171"))
                    ]
                else:
                    self._rows = rows
                    self._next_url = next_url
                    self._load_more.disabled = not bool(next_url)
                    self._status.value = f"{len(rows)} collection(s)" + (
                        " · more available" if next_url else ""
                    )
                    self._render_cards()
                self.update()
                if not err:
                    self._enrich_missing_thumbs()

            self._ui(apply)

        threading.Thread(target=work, daemon=True).start()

    def _scan_author(self) -> None:
        author = (self._author_scan.value or "").strip()
        if not author:
            self._status.value = "Enter an author name to scan collections."
            try:
                self.update()
            except Exception:
                pass
            return
        if self._on_author_scan:
            self._on_author_scan(author)
            return
        self._status.value = "Author scan not configured."
        try:
            self.update()
        except Exception:
            pass

    def show_author_scan_results(self, author: str, models: list[dict], *, collections_scanned: int = 0) -> None:
        """Open scan hits as a browsable model grid."""
        self._list_scroll_saved = self._scroll_offset
        self._list_panel.visible = False
        self._models_view.visible = True
        meta = {
            "UID": "",
            "Name": f"{author} — collection scan ({len(models)} models)",
            "Description": f"From {collections_scanned} public/subscribed collections",
        }
        self._models_view.open_model_list(meta, models, author_filter=author)
        try:
            self.update()
        except Exception:
            pass

    def _load_more_page(self) -> None:
        if self._loading or not self._next_url:
            return
        self._loading = True
        self._load_more.disabled = True
        self.update()
        next_url = self._next_url

        def work():
            err = None
            rows: list[dict] = []
            new_next = None
            try:
                data = self._client().search_collections(cursor_url=next_url)
                for item in data.get("results") or []:
                    rows.append(normalize_collection(item))
                new_next = data.get("next")
            except Exception as e:
                err = str(e)

            def apply():
                self._loading = False
                if err:
                    self._status.value = f"Load more failed: {err}"
                    self._load_more.disabled = False
                else:
                    seen = {r.get("UID") for r in self._rows}
                    for r in rows:
                        if r.get("UID") and r.get("UID") not in seen:
                            self._rows.append(r)
                            seen.add(r.get("UID"))
                    self._next_url = new_next
                    self._load_more.disabled = not bool(new_next)
                    self._status.value = f"{len(self._rows)} collection(s)" + (
                        " · more available" if new_next else ""
                    )
                    self._render_cards()
                self.update()
                if not err:
                    self._enrich_missing_thumbs()

            self._ui(apply)

        threading.Thread(target=work, daemon=True).start()

    def _enrich_missing_thumbs(self) -> None:
        """Fill empty collection cards with a first-model thumbnail."""
        need = [r for r in self._rows if r.get("UID") and not (r.get("Thumbnail") or "").strip()]
        if not need or not self._get_client:
            return

        def work():
            from browse_collections import unwrap_collection_model
            from browse_models import thumb_urls_from_api

            client = self._get_client()
            changed = False
            for row in need:
                uid = str(row.get("UID") or "")
                if not uid or (row.get("Thumbnail") or "").strip():
                    continue
                try:
                    data = client.list_collection_models(uid, count=1)
                    results = data.get("results") or []
                    if not results:
                        continue
                    model = unwrap_collection_model(results[0])
                    low, hi = thumb_urls_from_api(model)
                    url = low or hi
                    if not url:
                        continue
                    row["Thumbnail"] = url
                    changed = True
                except Exception:
                    continue
            if changed:
                def apply():
                    self._render_cards()
                    self.update()

                self._ui(apply)

        threading.Thread(target=work, daemon=True).start()

    def _render_cards(self) -> None:
        keep = self._scroll_offset
        if not self._rows:
            self.scroller.controls = [
                ft.Container(
                    padding=24,
                    content=ft.Text("No collections found.", size=13, color=_MUTED),
                )
            ]
            return
        rows = []
        for r in self._rows:
            probe = {
                "Name": r.get("Name") or "",
                "Tags": r.get("Name") or "",
                "Categories": "",
                "Assigned Collection(s)": r.get("Name") or "",
            }
            if passes_content_filter(
                probe,
                hide_nsfw=self._hide_nsfw,
                hide_female=self._hide_female,
                hide_male=self._hide_male,
            ):
                rows.append(r)
        if not rows:
            self.scroller.controls = [
                ft.Container(
                    padding=24,
                    content=ft.Text("All collections hidden by N / W / M toolbar filters.", size=13, color=_MUTED),
                )
            ]
            return
        self.scroller.controls = [self._collection_card(r) for r in rows]
        if keep > 0:
            try:
                self.scroller.scroll_to(offset=keep, duration=0)
            except Exception:
                pass

    def _collection_card(self, row: dict) -> ft.Container:
        uid = str(row.get("UID") or "")
        name = str(row.get("Name") or uid)
        author = str(row.get("Author") or "")
        thumb = str(row.get("Thumbnail") or "")
        models = _fmt_count(row.get("Model Count"))
        subs = _fmt_count(row.get("Subscriber Count"))
        desc = str(row.get("Description") or "").strip()
        url = str(row.get("URL") or "")
        subscribed = uid in self._subscribed_uids

        thumb_box = (
            ft.Image(src=thumb, width=120, height=90, fit=ft.ImageFit.COVER, border_radius=6)
            if thumb
            else ft.Container(
                width=120,
                height=90,
                bgcolor="#0f172a",
                border_radius=6,
                alignment=ft.alignment.center,
                content=ft.Icon(ft.Icons.COLLECTIONS, color=_MUTED),
            )
        )

        def open_models(_e=None, meta=row):
            self._open_models(meta)

        def toggle_sub(_e=None, meta=row, want=not subscribed):
            self._do_subscribe_async(meta, want)

        def open_author(_e=None, meta=row):
            key = str(meta.get("Author Username") or meta.get("Author") or "").strip()
            if key and self._on_open_account:
                self._on_open_account(key)

        author_key = str(row.get("Author Username") or author or "").strip()
        author_ctrl = (
            ft.TextButton(
                text=f"by {author}" if author else "Unknown author",
                style=ft.ButtonStyle(padding=0, color=_ACCENT),
                tooltip=f"Open @{author_key} in Account" if author_key else None,
                on_click=open_author,
            )
            if author_key and self._on_open_account
            else ft.Text(
                f"by {author}" if author else "Unknown author",
                size=12,
                color=_ACCENT,
                max_lines=1,
            )
        )

        return ft.Container(
            bgcolor=_CARD_BG,
            border_radius=10,
            padding=10,
            border=ft.border.all(1, "#334155"),
            content=ft.Row(
                [
                    ft.GestureDetector(content=thumb_box, on_tap=open_models),
                    ft.Column(
                        [
                            ft.Text(name, size=14, weight=ft.FontWeight.BOLD, color=_TEXT, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            author_ctrl,
                            ft.Text(
                                f"{models} models · {subs} subscribers",
                                size=11,
                                color=_MUTED,
                            ),
                            ft.Text(
                                desc[:160] + ("…" if len(desc) > 160 else "") if desc else "No description",
                                size=11,
                                color=_MUTED,
                                max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Row(
                                [
                                    ft.ElevatedButton(
                                        "Open",
                                        icon=ft.Icons.GRID_VIEW,
                                        height=32,
                                        on_click=open_models,
                                    ),
                                    ft.OutlinedButton(
                                        "Unsubscribe" if subscribed else "Subscribe",
                                        icon=ft.Icons.NOTIFICATIONS_OFF if subscribed else ft.Icons.NOTIFICATIONS_ACTIVE,
                                        height=32,
                                        on_click=toggle_sub,
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.OPEN_IN_NEW,
                                        tooltip="Open on Sketchfab",
                                        url=url or None,
                                        disabled=not bool(url),
                                    ),
                                ],
                                spacing=6,
                            ),
                        ],
                        spacing=3,
                        expand=True,
                        tight=True,
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )

    def open_by_meta(self, meta: dict) -> None:
        """Jump straight into a collection models view (from Account Subscribed, etc.)."""
        self._open_models(dict(meta or {}))

    def _open_models(self, meta: dict) -> None:
        self._list_scroll_saved = self._scroll_offset
        self._list_panel.visible = False
        self._models_view.visible = True
        self._models_view.set_subscribed_uids(self._subscribed_uids)
        author = (self._author_scan.value or "").strip()
        self._models_view.open_collection(meta, author_filter=author)
        self.update()

    def _close_models(self) -> None:
        self._models_view.cancel_pending_loads()
        self._models_view.visible = False
        self._list_panel.visible = True
        off = self._list_scroll_saved if self._list_scroll_saved > 0 else self._scroll_offset
        try:
            self.scroller.scroll_to(offset=off, duration=0)
        except Exception:
            pass
        self._scroll_offset = off
        try:
            self.update()
        except Exception:
            pass

    def _subscribe_collection(self, meta: dict, want: bool) -> None:
        if not self._on_subscribe:
            raise RuntimeError("Subscribe not configured")
        self._on_subscribe(meta, want)
        uid = str((meta or {}).get("UID") or "").strip()
        if not uid:
            raise ValueError("Collection UID required")
        if want:
            self._subscribed_uids.add(uid)
        else:
            self._subscribed_uids.discard(uid)
        if self._on_subscribe_ui:
            hook = self._on_subscribe_ui

            def _after():
                hook()
                self._models_view.set_subscribed_uids(self._subscribed_uids)
                self._render_cards()

            self._ui(_after)

    def _do_subscribe(self, meta: dict, want: bool) -> None:
        """Called from CollectionModelsView worker thread — may raise."""
        if self._on_subscribe:
            self._on_subscribe(meta, want)
        uid = str(meta.get("UID") or "")
        if want:
            self._subscribed_uids.add(uid)
        else:
            self._subscribed_uids.discard(uid)

    def _do_subscribe_async(self, meta: dict, want: bool) -> None:
        uid = str(meta.get("UID") or "")
        if not uid:
            return

        def work():
            err = None
            try:
                self._do_subscribe(meta, want)
            except Exception as e:
                err = str(e)

            def apply():
                if err:
                    self._status.value = f"Subscribe failed: {err}"
                else:
                    self._status.value = "Subscribed." if want else "Unsubscribed."
                    self._models_view.set_subscribed_uids(self._subscribed_uids)
                    self._render_cards()
                self.update()

            self._ui(apply)

        threading.Thread(target=work, daemon=True).start()
