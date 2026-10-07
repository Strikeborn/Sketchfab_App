"""My Collections tab — list your collections and open them as model cards."""
from __future__ import annotations

import math
import threading

import flet as ft
import pandas as pd

from browse_collections import normalize_collection, unwrap_collection_model
from browse_models import thumb_urls_from_api
from collection_urls import collection_public_url
from content_filter import filter_collections_df
from ui.collection_models_view import CollectionModelsView
from ui.scroll_drag import wrap_middle_drag_scroll

_HDR_BG = "#1e293b"
_HDR_BORDER = "#475569"
_ROW_BORDER = "#334155"
_COL_SPACING = 12
_THUMB_SZ = 48
_SKIP_COLS = {"Thumbnail", "Thumbnail HD"}

_COL_WIDTHS = {
    "Collection Name": 200,
    "Collection UID": 220,
    "Slug": 120,
    "Models": 72,
    "Model Count": 72,
    "Model Names": 200,
}


def _fmt(v):
    return "" if pd.isna(v) else str(v)


def _header_label(col: str) -> str:
    if col == "Model Count":
        return "Models"
    return col


def _cell(col: str, val, uid: str = "", slug: str = "") -> ft.Container:
    text = _fmt(val)
    if col == "Collection Name":
        url = collection_public_url(text, uid, slug=slug)
        return ft.Container(
            width=_COL_WIDTHS.get(col, 140),
            content=ft.TextButton(text=text, url=url, style=ft.ButtonStyle(padding=0)) if url else ft.Text(text, size=12),
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
            alignment=ft.alignment.center_left,
        )
    if col == "Model Names":
        # Compact preview — full list used to blow row height to a wall of text.
        names = [p.strip() for p in text.split(",") if p.strip()] if text else []
        preview = ", ".join(names[:3])
        if len(names) > 3:
            preview = f"{preview}…"
        tip = text if len(text) < 2000 else (", ".join(names[:40]) + "…")
        return ft.Container(
            width=_COL_WIDTHS.get(col, 200),
            height=40,
            content=ft.Text(
                preview or "—",
                size=11,
                color=ft.Colors.GREY_400,
                max_lines=2,
                overflow=ft.TextOverflow.ELLIPSIS,
                tooltip=tip or None,
            ),
            padding=ft.padding.symmetric(horizontal=6, vertical=6),
            alignment=ft.alignment.center_left,
            bgcolor="#0f172a",
            border_radius=6,
        )
    if col in ("Model Count", "Models"):
        try:
            n = int(float(text)) if text not in ("", "nan", "None") else 0
            label = f"{n:,}"
        except (TypeError, ValueError):
            label = text or "—"
        return ft.Container(
            width=_COL_WIDTHS.get(col, 72),
            content=ft.Text(label, size=12, text_align=ft.TextAlign.RIGHT),
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
            alignment=ft.alignment.center_right,
        )
    width = _COL_WIDTHS.get(col, 140)
    return ft.Container(
        width=width,
        content=ft.Text(text, size=12, selectable=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
        padding=ft.padding.symmetric(horizontal=6, vertical=8),
        alignment=ft.alignment.center_left,
    )


def _thumb_box(url: str = "") -> ft.Container:
    if url:
        return ft.Container(
            width=_THUMB_SZ,
            height=_THUMB_SZ,
            border_radius=6,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            bgcolor="#0f172a",
            content=ft.Image(src=url, width=_THUMB_SZ, height=_THUMB_SZ, fit=ft.ImageFit.COVER),
        )
    return ft.Container(
        width=_THUMB_SZ,
        height=_THUMB_SZ,
        border_radius=6,
        bgcolor="#0f172a",
        alignment=ft.alignment.center,
        content=ft.Icon(ft.Icons.COLLECTIONS, size=22, color=ft.Colors.GREY_600),
    )


def _header_bar(cols: list[str]) -> ft.Container:
    cells = [
        ft.Container(width=56, content=ft.Text("", size=12), padding=ft.padding.symmetric(horizontal=2, vertical=8)),
        ft.Container(width=_THUMB_SZ + 8, content=ft.Text("", size=12), padding=ft.padding.symmetric(horizontal=2, vertical=8)),
    ]
    for c in cols:
        if c in _SKIP_COLS:
            continue
        label = _header_label(c)
        width = _COL_WIDTHS.get(c, 140)
        cells.append(
            ft.Container(
                width=width,
                content=ft.Text(label, weight=ft.FontWeight.BOLD, size=12, max_lines=1),
                padding=ft.padding.only(left=6, right=6, top=8, bottom=8),
            )
        )
    return ft.Container(
        content=ft.Row(cells, spacing=_COL_SPACING, expand=True),
        bgcolor=_HDR_BG,
        border=ft.border.only(bottom=ft.BorderSide(1, _HDR_BORDER)),
    )


def _safe_update(ctrl: ft.Control) -> None:
    """Flet asserts if update() walks a child that was never mounted (e.g. inactive tab)."""
    try:
        if getattr(ctrl, "page", None) is None:
            return
        ctrl.update()
    except (AssertionError, RuntimeError):
        pass
    except Exception:
        pass


class CollectionsTab(ft.Column):
    def __init__(self):
        super().__init__(expand=True, spacing=6)
        self._source_df = pd.DataFrame()
        self._liked_df = pd.DataFrame()
        self._thumb_by_uid: dict[str, str] = {}
        self._thumb_by_name: dict[str, str] = {}
        self._hide_nsfw = True
        self._hide_female = True
        self._hide_male = True
        self._page_ref: ft.Page | None = None
        self._get_client = None
        self._on_preview = None
        self._on_download = None
        self._on_subscribe = None
        self._on_remove = None
        self._subscribed_uids: set[str] = set()
        self._thumb_imgs: dict[str, ft.Container] = {}
        self._thumb_fetch_gen = 0

        self._search = ft.TextField(
            label="Search collections",
            hint_text="Name, UID, or model in collection…",
            width=320,
            dense=True,
            text_size=12,
            on_submit=lambda e: self._apply_search(),
        )
        self._count_label = ft.Text("", size=11, color=ft.Colors.GREY_500)
        self.pager = ft.Row()
        self.header_bar = ft.Container()
        self.scroller = ft.Column(scroll=ft.ScrollMode.ALWAYS, expand=True, spacing=0)
        self._scroller_wrap = wrap_middle_drag_scroll(self.scroller, expand=True)

        self._list_panel = ft.Column(
            expand=True,
            spacing=6,
            controls=[
                ft.Row(
                    [
                        self._search,
                        ft.IconButton(icon=ft.Icons.SEARCH, tooltip="Filter", on_click=lambda e: self._apply_search()),
                        ft.TextButton("Clear", on_click=lambda e: self._clear_search()),
                        ft.Container(expand=True),
                        self._count_label,
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.pager,
                self.header_bar,
                self._scroller_wrap,
            ],
        )

        self._models_view = CollectionModelsView(
            on_back=self._close_models,
            on_preview=lambda uid: self._on_preview(uid) if self._on_preview else None,
            on_download=lambda uid, name: self._on_download(uid, name) if self._on_download else None,
            on_subscribe=lambda meta, want: self._on_subscribe(meta, want) if self._on_subscribe else None,
            on_remove=lambda model_uid, coll_uid, meta: self._on_remove(model_uid, coll_uid, meta)
            if self._on_remove
            else None,
            get_client=lambda: self._get_client() if self._get_client else None,
            allow_remove=True,
        )
        self._models_view.visible = False

        self.controls = [self._list_panel, self._models_view]

    def set_page(self, page: ft.Page) -> None:
        self._page_ref = page
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
        self._models_view.set_open_account_callback(on_open_account)

    def scroll_to_top(self) -> None:
        if getattr(self._models_view, "visible", False):
            self._models_view.scroll_to_top()
            return
        try:
            self.scroller.scroll_to(offset=0, duration=200)
        except Exception:
            pass

    def set_like_callback(self, cb) -> None:
        self._models_view.set_like_callback(cb)

    def set_liked_uids(self, uids: set[str]) -> None:
        self._models_view.set_liked_uids(uids)

    def mark_liked(self, uid: str) -> None:
        self._models_view.mark_liked(uid)

    def set_subscribe_callback(self, cb) -> None:
        self._on_subscribe = cb

    def set_remove_callback(self, cb) -> None:
        self._on_remove = cb

    def set_subscribed_uids(self, uids: set[str]) -> None:
        self._subscribed_uids = set(uids or set())
        self._models_view.set_subscribed_uids(self._subscribed_uids)

    def set_liked_df(self, liked_df: pd.DataFrame | None) -> None:
        self._liked_df = liked_df if isinstance(liked_df, pd.DataFrame) else pd.DataFrame()
        self._rebuild_thumb_cache_from_likes()

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        # Flags only — caller (refresh_tables) rebuilds; avoid double set_df + update races.
        self._hide_nsfw = hide_nsfw
        self._hide_female = hide_female
        self._hide_male = hide_male
        try:
            self._models_view.set_content_filters(hide_nsfw, hide_female, hide_male)
        except Exception:
            pass

    def set_search_query(self, query: str) -> None:
        self._search.value = query or ""
        self._apply_search()

    def _clear_search(self) -> None:
        self._search.value = ""
        self._apply_search()

    def _rebuild_thumb_cache_from_likes(self) -> None:
        """Map collection name → first liked-model thumbnail that belongs to it."""
        self._thumb_by_name = {}
        df = self._liked_df
        if df is None or df.empty or "Thumbnail" not in df.columns:
            return
        name_cols = [c for c in ("Already In Collection(s)", "Assigned Collection(s)") if c in df.columns]
        if not name_cols:
            return
        for _, row in df.iterrows():
            thumb = str(row.get("Thumbnail") or row.get("Thumbnail HD") or "").strip()
            if not thumb or thumb.lower() in {"nan", "none", "<na>"}:
                continue
            for col in name_cols:
                raw = str(row.get(col) or "")
                for part in raw.replace(";", ",").split(","):
                    name = part.strip()
                    if not name:
                        continue
                    key = name.casefold()
                    if key not in self._thumb_by_name:
                        self._thumb_by_name[key] = thumb

    def _thumb_for(self, name: str, uid: str, row_thumb: str = "") -> str:
        t = (row_thumb or "").strip()
        if t and t.lower() not in {"nan", "none", "<na>"}:
            return t
        if uid and uid in self._thumb_by_uid:
            return self._thumb_by_uid[uid]
        key = (name or "").strip().casefold()
        if key and key in self._thumb_by_name:
            return self._thumb_by_name[key]
        return ""

    def _filtered_df(self) -> pd.DataFrame:
        df = filter_collections_df(
            self._source_df,
            hide_nsfw=self._hide_nsfw,
            hide_female=self._hide_female,
            hide_male=self._hide_male,
        )
        if df is None or df.empty:
            return pd.DataFrame()
        q = (self._search.value or "").strip().lower()
        if not q:
            return df
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            if col in _SKIP_COLS:
                continue
            mask |= df[col].fillna("").astype(str).str.lower().str.contains(q, regex=False)
        return df.loc[mask]

    def _apply_search(self) -> None:
        if self._source_df is None or self._source_df.empty:
            return
        self.set_df(self._source_df, 0, getattr(self, "_page_size", 100))

    def open_by_meta(self, meta: dict) -> None:
        """Open a collection card view from Account / Report (public entry)."""
        meta = meta or {}
        self._open_collection_row(
            str(meta.get("Name") or ""),
            str(meta.get("UID") or ""),
            str(meta.get("Slug") or meta.get("slug") or ""),
            meta.get("Model Count") or meta.get("modelCount"),
            str(meta.get("Thumbnail") or ""),
        )

    def _open_collection_row(
        self,
        name: str,
        uid: str,
        slug: str = "",
        model_count=None,
        thumb: str = "",
    ) -> None:
        uid = str(uid or "").strip()
        if not uid:
            return
        meta = normalize_collection(
            {
                "uid": uid,
                "name": name,
                "slug": slug,
                "modelCount": model_count,
                "thumbnails": {"images": [{"url": thumb, "width": 256}]} if thumb else {},
            }
        )
        if thumb:
            meta["Thumbnail"] = thumb
        self._list_panel.visible = False
        self._models_view.visible = True
        self._models_view.set_subscribed_uids(self._subscribed_uids)
        self._models_view.open_collection(meta)
        _safe_update(self)

    def _close_models(self) -> None:
        self._models_view.visible = False
        self._list_panel.visible = True
        _safe_update(self)

    def _data_row(
        self,
        cols: list[str],
        values: tuple,
        uid: str = "",
        slug: str = "",
        thumb: str = "",
    ) -> ft.Container:
        slug_val = slug
        name = ""
        model_count = None
        cells: list[ft.Control] = []
        for c, v in zip(cols, values):
            if c in _SKIP_COLS:
                continue
            if c == "Slug" and not slug_val:
                slug_val = str(v).strip()
            if c == "Collection Name":
                name = _fmt(v)
            if c == "Model Count":
                try:
                    model_count = int(v) if v is not None and str(v).strip() != "" else None
                except (TypeError, ValueError):
                    model_count = None
            cells.append(_cell(c, v, uid=uid, slug=slug_val))

        thumb_ctrl = _thumb_box(thumb)
        if uid:
            self._thumb_imgs[uid] = thumb_ctrl

        open_btn = ft.TextButton(
            content=ft.Text("Open", size=12, no_wrap=True),
            style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=2)),
            tooltip="Browse models as cards",
            on_click=lambda e, n=name, u=uid, s=slug_val, mc=model_count, th=thumb: self._open_collection_row(
                n, u, s, mc, th
            ),
        )
        cells.insert(
            0,
            ft.Container(
                width=_THUMB_SZ + 8,
                content=thumb_ctrl,
                padding=ft.padding.symmetric(horizontal=2, vertical=4),
                alignment=ft.alignment.center,
            ),
        )
        cells.insert(
            0,
            ft.Container(
                width=56,
                content=open_btn,
                padding=ft.padding.symmetric(horizontal=0, vertical=4),
                alignment=ft.alignment.center_left,
            ),
        )
        return ft.Container(
            content=ft.Row(cells, spacing=_COL_SPACING, expand=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            border=ft.border.only(bottom=ft.BorderSide(1, _ROW_BORDER)),
        )

    def _paint_thumb(self, uid: str, url: str) -> None:
        box = self._thumb_imgs.get(uid)
        if not box or not url:
            return
        box.content = ft.Image(src=url, width=_THUMB_SZ, height=_THUMB_SZ, fit=ft.ImageFit.COVER)
        try:
            if getattr(box, "page", None):
                box.update()
        except Exception:
            pass

    def _fetch_missing_thumbs(self, need: list[tuple[str, str]]) -> None:
        """Background: pull one model thumb per collection missing an image."""
        if not need or not self._get_client:
            return
        self._thumb_fetch_gen += 1
        gen = self._thumb_fetch_gen

        def work():
            client = self._get_client()
            for uid, name in need:
                if gen != self._thumb_fetch_gen:
                    return
                if not uid or self._thumb_for(name, uid):
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
                    self._thumb_by_uid[uid] = url
                    if name:
                        self._thumb_by_name[name.casefold()] = url
                    # Persist into source df if possible
                    if (
                        not self._source_df.empty
                        and "Collection UID" in self._source_df.columns
                    ):
                        if "Thumbnail" not in self._source_df.columns:
                            self._source_df["Thumbnail"] = ""
                        mask = self._source_df["Collection UID"].astype(str) == uid
                        self._source_df.loc[mask, "Thumbnail"] = url

                    def paint(u=uid, src=url):
                        self._paint_thumb(u, src)

                    if self._page_ref:
                        async def _ui(fn=paint):
                            fn()

                        try:
                            self._page_ref.run_task(_ui)
                        except Exception:
                            paint()
                    else:
                        paint()
                except Exception:
                    continue

        threading.Thread(target=work, daemon=True).start()

    def set_df(self, df, page_idx: int = 0, page_size: int = 100):
        self._source_df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        self._page_size = page_size
        self._thumb_imgs = {}
        dff = self._filtered_df()
        if dff.empty:
            filt_note = ""
            if self._hide_nsfw or self._hide_female or self._hide_male:
                filt_note = " (N/W/M filters may be hiding collections)"
            self.pager.controls = [ft.Text("0 rows")]
            self.header_bar.content = ft.Text(
                ("No collections match" if self._search.value else "No data yet") + filt_note,
                size=12,
            )
            self.scroller.controls = []
            self._count_label.value = ""
            _safe_update(self)
            return

        total = len(dff)
        pages = max(1, math.ceil(total / page_size))
        page_idx = max(0, min(page_idx, pages - 1))
        dfp = dff.iloc[page_idx * page_size : min((page_idx + 1) * page_size, total)]

        cols = list(dff.columns)
        uid_col = "Collection UID" if "Collection UID" in cols else None
        slug_col = "Slug" if "Slug" in cols else None
        name_col = "Collection Name" if "Collection Name" in cols else None
        thumb_col = "Thumbnail" if "Thumbnail" in cols else None
        # Mutate existing header in place — replacing controls[i] with a fresh
        # Container leaves Flet with children that have no __uid yet.
        new_header = _header_bar(cols)
        self.header_bar.content = new_header.content
        self.header_bar.bgcolor = new_header.bgcolor
        self.header_bar.border = new_header.border
        rows = []
        missing: list[tuple[str, str]] = []
        for row in dfp.itertuples(index=False, name=None):
            uid = ""
            slug = ""
            name = ""
            row_thumb = ""
            if uid_col:
                try:
                    uid = str(row[cols.index(uid_col)])
                except (ValueError, IndexError):
                    pass
            if slug_col:
                try:
                    slug = str(row[cols.index(slug_col)])
                except (ValueError, IndexError):
                    pass
            if name_col:
                try:
                    name = str(row[cols.index(name_col)])
                except (ValueError, IndexError):
                    pass
            if thumb_col:
                try:
                    row_thumb = str(row[cols.index(thumb_col)] or "")
                except (ValueError, IndexError):
                    pass
            thumb = self._thumb_for(name, uid, row_thumb)
            if not thumb and uid:
                missing.append((uid, name))
            rows.append(self._data_row(cols, row, uid=uid, slug=slug, thumb=thumb))
        self.scroller.controls = rows
        qnote = f"  ·  filtered '{self._search.value}'" if (self._search.value or "").strip() else ""
        self._count_label.value = f"{total:,} collections{qnote}"
        _safe_update(self)
        try:
            self.scroller.scroll_to(offset=0, duration=0)
        except Exception:
            pass
        if missing:
            self._fetch_missing_thumbs(missing)
