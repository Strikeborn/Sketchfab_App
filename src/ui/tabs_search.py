from __future__ import annotations

import math

import flet as ft
import pandas as pd

from search_filter import filter_liked_df, list_authors, list_categories, list_collection_names, list_tags
from ui.thumb_lod import make_lod_thumb, thumb_urls_from_row, upgrade_thumbnails

_PANEL = "#0f172a"
_ROW_ALT = "#1a2332"
_ROW_BORDER = "#334155"


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"none", "nan", "<na>"} else s


def _model_name(row: pd.Series) -> str:
    return _fmt(row.get("Name") or row.get("Model Name"))


def _model_uid(row: pd.Series) -> str:
    return _fmt(row.get("UID") or row.get("Model UID"))


def _model_url(row: pd.Series) -> str | None:
    uid = _model_uid(row)
    return f"https://sketchfab.com/3d-models/{uid}" if uid else None


class SearchTab(ft.Container):
    def __init__(self, page: ft.Page | None = None):
        self._page = page
        self._source_df = pd.DataFrame()
        self._colls_df = pd.DataFrame()
        self._filtered = pd.DataFrame()
        self._page_slice = pd.DataFrame()
        self._load_token = 0
        self._lod_queue: list[tuple[ft.Image, str, str]] = []
        self._view_mode = "table"
        self._on_filter_change = None
        self._on_assign = None
        self._on_bulk_assign = None

        self._query = ft.TextField(
            label="Search text",
            hint_text="Name, UID, author, tags…",
            width=260,
            dense=True,
            text_size=12,
            on_submit=lambda e: self._apply_filters(),
        )
        self._author_dd = ft.Dropdown(label="Author", width=150, text_size=12, dense=True, value="(all)", options=[ft.dropdown.Option("(all)")])
        self._category_dd = ft.Dropdown(label="Category", width=150, text_size=12, dense=True, value="(all)", options=[ft.dropdown.Option("(all)")])
        self._tag_dd = ft.Dropdown(label="Tag", width=130, text_size=12, dense=True, value="(all)", options=[ft.dropdown.Option("(all)")])
        self._collection_dd = ft.Dropdown(label="In collection", width=150, text_size=12, dense=True, value="(all)", options=[ft.dropdown.Option("(all)")])
        self._assignment_dd = ft.Dropdown(
            label="Status",
            width=120,
            text_size=12,
            dense=True,
            value="all",
            options=[
                ft.dropdown.Option("all", "All"),
                ft.dropdown.Option("unassigned", "Unassigned"),
                ft.dropdown.Option("assigned", "Assigned"),
                ft.dropdown.Option("in_collection", "In collection"),
                ft.dropdown.Option("unlisted", "Unlisted (private)"),
            ],
        )
        self._dl_only = ft.Checkbox(label="Downloadable", value=False)
        self._disk_only = ft.Checkbox(label="On disk", value=False)
        self._result_label = ft.Text("0 results", size=12, color=ft.Colors.GREY_400)

        self._assign_dd = ft.Dropdown(label="Assign to collection", width=200, text_size=12, dense=True, options=[])
        self._bulk_assign_btn = ft.ElevatedButton(
            "Assign all filtered",
            icon=ft.Icons.FOLDER_SHARED,
            on_click=lambda e: self._bulk_assign(),
        )

        self._view_seg = ft.SegmentedButton(
            selected={"table"},
            segments=[
                ft.Segment(value="grid", label=ft.Text("Grid"), icon=ft.Icon(ft.Icons.GRID_VIEW)),
                ft.Segment(value="table", label=ft.Text("Table"), icon=ft.Icon(ft.Icons.TABLE_ROWS)),
                ft.Segment(value="author", label=ft.Text("By author"), icon=ft.Icon(ft.Icons.PEOPLE)),
            ],
            on_change=self._on_view_change,
        )

        self.pager = ft.Row()
        self._results = ft.ListView(expand=True, spacing=0, padding=0)

        filter_panel = ft.Container(
            padding=10,
            bgcolor=_PANEL,
            border_radius=8,
            content=ft.Column(
                [
                    ft.Text("Search & filters", size=13, weight=ft.FontWeight.BOLD),
                    ft.Row(
                        [self._query, self._author_dd, self._category_dd, self._tag_dd, self._collection_dd, self._assignment_dd],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        [
                            self._dl_only,
                            self._disk_only,
                            ft.ElevatedButton("Search", icon=ft.Icons.SEARCH, on_click=lambda e: self._apply_filters()),
                            ft.OutlinedButton("Clear", on_click=lambda e: self._clear_filters()),
                            ft.Container(expand=True),
                            self._view_seg,
                            self._result_label,
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        wrap=True,
                    ),
                    ft.Divider(height=1, color="#334155"),
                    ft.Text("Assign filtered results to a collection (saved locally — Push to sync Sketchfab)", size=10, color=ft.Colors.GREY_500),
                    ft.Row(
                        [self._assign_dd, self._bulk_assign_btn],
                        spacing=8,
                        wrap=True,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
        )

        super().__init__(
            expand=True,
            padding=0,
            content=ft.Column(
                [filter_panel, self.pager, self._results],
                expand=True,
                spacing=6,
            ),
        )

        for ctrl in (self._author_dd, self._category_dd, self._tag_dd, self._collection_dd, self._assignment_dd):
            ctrl.on_change = lambda e: self._apply_filters()
        self._dl_only.on_change = lambda e: self._apply_filters()
        self._disk_only.on_change = lambda e: self._apply_filters()

    def set_page(self, page: ft.Page) -> None:
        self._page = page

    def set_assign_callbacks(self, on_assign, on_bulk_assign) -> None:
        self._on_assign = on_assign
        self._on_bulk_assign = on_bulk_assign

    def _bulk_assign(self) -> None:
        coll = self._assign_dd.value
        if not coll or not self._on_bulk_assign:
            return
        uids = [_model_uid(row) for _, row in self._filtered.iterrows() if _model_uid(row)]
        if uids:
            self._on_bulk_assign(uids, coll)

    def _assign_one(self, uid: str) -> None:
        coll = self._assign_dd.value
        if uid and coll and self._on_assign:
            self._on_assign(uid, coll)

    def _on_view_change(self, e) -> None:
        sel = getattr(e.control, "selected", None) or {"table"}
        if isinstance(sel, set) and sel:
            self._view_mode = next(iter(sel))
        self._lod_queue = []
        self._render_results()
        self.update()
        if self._lod_queue and self._page:
            self._page.run_task(self._upgrade_lod, self._load_token)

    def _filter_kwargs(self) -> dict:
        coll = self._collection_dd.value or "(all)"
        tag = self._tag_dd.value or "(all)"
        return {
            "query": self._query.value or "",
            "author": self._author_dd.value or "(all)",
            "category": self._category_dd.value or "(all)",
            "tag": tag if tag != "(all)" else "",
            "assignment": self._assignment_dd.value or "all",
            "downloadable_only": bool(self._dl_only.value),
            "on_disk_only": bool(self._disk_only.value),
            "collection": coll if coll != "(all)" else "",
        }

    def _run_filter(self) -> pd.DataFrame:
        return filter_liked_df(self._source_df, **self._filter_kwargs())

    def _clear_filters(self) -> None:
        self._query.value = ""
        self._author_dd.value = "(all)"
        self._category_dd.value = "(all)"
        self._tag_dd.value = "(all)"
        self._collection_dd.value = "(all)"
        self._assignment_dd.value = "all"
        self._dl_only.value = False
        self._disk_only.value = False
        self._apply_filters()

    def _apply_filters(self) -> None:
        if self._on_filter_change:
            self._on_filter_change(0)

    def set_filter_callback(self, cb) -> None:
        self._on_filter_change = cb

    def _refresh_dropdowns(self) -> None:
        self._author_dd.options = [ft.dropdown.Option("(all)")] + [ft.dropdown.Option(a) for a in list_authors(self._source_df)]
        self._category_dd.options = [ft.dropdown.Option("(all)")] + [ft.dropdown.Option(c) for c in list_categories(self._source_df)]
        self._tag_dd.options = [ft.dropdown.Option("(all)")] + [ft.dropdown.Option(t) for t in list_tags(self._source_df)]
        names = list_collection_names(self._colls_df)
        self._collection_dd.options = [ft.dropdown.Option("(all)")] + [ft.dropdown.Option(c) for c in names]
        self._assign_dd.options = [ft.dropdown.Option(c) for c in names]
        if names and not self._assign_dd.value:
            self._assign_dd.value = names[0]

    def set_df(self, df: pd.DataFrame, page_idx: int = 0, page_size: int = 48, colls_df: pd.DataFrame | None = None) -> None:
        self._source_df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        if colls_df is not None:
            self._colls_df = colls_df
        self._refresh_dropdowns()
        self._filtered = self._run_filter()
        self._result_label.value = f"{len(self._filtered):,} of {len(self._source_df):,}"

        if self._filtered.empty:
            self._results.controls = [
                ft.Container(
                    padding=20,
                    content=ft.Text("No matches — try broader filters or Collect first.", size=13, color=ft.Colors.GREY_500),
                )
            ]
            self.update()
            return

        total = len(self._filtered)
        pages = max(1, math.ceil(total / page_size))
        page_idx = max(0, min(page_idx, pages - 1))
        self._page_slice = self._filtered.iloc[page_idx * page_size : min((page_idx + 1) * page_size, total)]
        self._load_token += 1
        self._lod_queue = []
        self._render_results()
        self.update()
        if self._lod_queue and self._page:
            self._page.run_task(self._upgrade_lod, self._load_token)

    def _render_results(self) -> None:
        dfp = self._page_slice if isinstance(self._page_slice, pd.DataFrame) else pd.DataFrame()
        if dfp.empty:
            self._results.controls = []
            return

        if self._view_mode == "author":
            self._results.controls = self._build_author_groups(dfp)
            self._results.spacing = 8
            self._results.padding = 4
            return

        if self._view_mode == "grid":
            cards = [self._grid_card(row) for _, row in dfp.iterrows()]
            self._results.controls = [
                ft.Container(
                    padding=8,
                    content=ft.Row(cards, wrap=True, spacing=8, run_spacing=8),
                )
            ]
            self._results.spacing = 0
            return

        self._results.controls = [self._table_row(row, i % 2 == 0) for i, (_, row) in enumerate(dfp.iterrows())]
        self._results.spacing = 0

    async def _upgrade_lod(self, token: int) -> None:
        await upgrade_thumbnails(self._lod_queue, token, self._load_token, root=self)

    def _grid_card(self, row: pd.Series) -> ft.Container:
        low, hi = thumb_urls_from_row(row)
        thumb_box, img = make_lod_thumb(low, hi, size=100)
        if img and img.src:
            self._lod_queue.append((img, low, hi))
        name = _model_name(row)
        url = _model_url(row)
        uid = _model_uid(row)
        title = ft.TextButton(text=name[:28], url=url) if url else ft.Text(name[:28], size=11)
        return ft.Container(
            width=120,
            padding=6,
            bgcolor=_PANEL,
            border_radius=8,
            content=ft.Column(
                [
                    thumb_box,
                    title,
                    ft.Text(_fmt(row.get("Author")) or "-", size=9, color=ft.Colors.GREY_500, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.IconButton(icon=ft.Icons.FOLDER, icon_size=16, tooltip="Assign to selected collection", on_click=lambda e, u=uid: self._assign_one(u)),
                ],
                spacing=2,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
        )

    def _table_row(self, row: pd.Series, even: bool) -> ft.Container:
        low, hi = thumb_urls_from_row(row)
        thumb_box, img = make_lod_thumb(low, hi, size=56)
        if img and img.src:
            self._lod_queue.append((img, low, hi))
        name = _model_name(row)
        url = _model_url(row)
        uid = _model_uid(row)
        title = ft.TextButton(text=name, url=url) if url else ft.Text(name, size=12)
        assigned = _fmt(row.get("Assigned Collection(s)"))
        return ft.Container(
            bgcolor=_ROW_ALT if even else None,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            border=ft.border.only(bottom=ft.BorderSide(1, _ROW_BORDER)),
            content=ft.Row(
                [
                    thumb_box,
                    ft.Column(
                        [
                            title,
                            ft.Text(f"{_fmt(row.get('Author'))}  ·  tags: {_fmt(row.get('Tags'))[:50]}", size=10, color=ft.Colors.GREY_500, max_lines=1),
                            ft.Text(f"assigned: {assigned or '-'}", size=10, color=ft.Colors.GREY_600),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    ft.IconButton(icon=ft.Icons.FOLDER, tooltip="Assign to collection above", on_click=lambda e, u=uid: self._assign_one(u)),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _build_author_groups(self, dfp: pd.DataFrame) -> list[ft.Control]:
        if "Author" not in dfp.columns:
            return [ft.Text("No Author column.", size=12)]
        groups: list[ft.Control] = []
        authors = dfp["Author"].fillna("").astype(str).str.strip().replace("", "(unknown)")
        for author in sorted(authors.unique(), key=lambda x: x.lower()):
            subset = dfp.loc[authors == author]
            cards = []
            for _, row in subset.iterrows():
                low, hi = thumb_urls_from_row(row)
                thumb_box, img = make_lod_thumb(low, hi, size=64)
                if img and img.src:
                    self._lod_queue.append((img, low, hi))
                name = _model_name(row)
                url = _model_url(row)
                uid = _model_uid(row)
                label = ft.TextButton(text=name[:20], url=url) if url else ft.Text(name[:20], size=10)
                cards.append(
                    ft.Container(
                        width=80,
                        content=ft.Column(
                            [thumb_box, label, ft.IconButton(icon=ft.Icons.FOLDER, icon_size=14, on_click=lambda e, u=uid: self._assign_one(u))],
                            spacing=2,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            tight=True,
                        ),
                    )
                )
            groups.append(
                ft.Container(
                    padding=8,
                    bgcolor=_PANEL,
                    border_radius=8,
                    content=ft.Column(
                        [ft.Text(f"{author}  ({len(subset)})", size=12, weight=ft.FontWeight.BOLD), ft.Row(cards, spacing=6, wrap=True)],
                        spacing=6,
                        tight=True,
                    ),
                )
            )
        return groups
