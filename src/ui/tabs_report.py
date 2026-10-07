from __future__ import annotations

import os

import flet as ft
import pandas as pd

from content_filter import filter_collections_df, filter_dataframe
from matching import Terms
from tag_vetting import filter_term_items, term_status, vet_term as save_vet_term
from collection_urls import collection_public_url
from ui.scroll_drag import wrap_middle_drag_scroll
from report_stats import (
    bulk_assignment_candidates,
    mine_author_tags,
    mine_categories,
    mine_fuzzy_hits,
    mine_name_tokens,
    mine_suggested_terms,
    unassigned_df,
)

_CARD_BG = "#1e293b"
_PANEL_BG = "#0f172a"
_BULK_WIDTH = 460
_TERMS_PATH = os.environ.get("TERMS_PATH", os.path.join("terms", "collections_terms.yaml"))

# (header, width, right_align)
_BULK_COLS: list[tuple[str, int, bool]] = [
    ("Collection", 116, False),
    ("Total", 46, True),
    ("Tags", 40, True),
    ("Title", 40, True),
    ("Sugg", 40, True),
    ("Fuzzy", 44, True),
    ("YAML", 44, True),
    ("Strength", 72, False),
]
_COL_GAP = 6


def _bulk_header_row() -> ft.Row:
    cells = []
    for label, w, right in _BULK_COLS:
        cells.append(
            ft.Container(
                width=w,
                content=ft.Text(
                    label,
                    size=11,
                    weight=ft.FontWeight.BOLD,
                    color=ft.Colors.GREY_300,
                    text_align=ft.TextAlign.RIGHT if right else ft.TextAlign.LEFT,
                ),
            )
        )
    return ft.Row(cells, spacing=_COL_GAP)


def _bulk_data_row(row: dict, color, bg, on_open_collection=None) -> ft.Container:
    coll = row["collection"]
    cuid = row.get("collection_uid") or ""
    url = collection_public_url(row["collection"], cuid, slug=row.get("collection_slug"))
    coll_ctrl = (
        ft.TextButton(
            text=coll[:15],
            url=url,
            style=ft.ButtonStyle(padding=0),
            on_click=lambda e, n=coll: on_open_collection(n) if on_open_collection and not url else None,
        )
        if url or on_open_collection
        else ft.Text(coll[:15], size=11, font_family="Consolas", color=color)
    )
    vals = [
        None,
        f"{row['total']:,}",
        str(row["tag_hit"]),
        str(row["name_hit"]),
        str(row["suggested"]),
        str(row["fuzzy"]),
        str(row["rules"]),
        row["confidence"],
    ]
    cells = []
    for i, ((_label, w, right), val) in enumerate(zip(_BULK_COLS, vals)):
        if i == 0:
            cells.append(ft.Container(width=w, content=coll_ctrl))
        else:
            cells.append(
                ft.Container(
                    width=w,
                    content=ft.Text(
                        val,
                        size=11,
                        font_family="Consolas",
                        color=color,
                        text_align=ft.TextAlign.RIGHT if right else ft.TextAlign.LEFT,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        max_lines=1,
                    ),
                )
            )
    return ft.Container(
        bgcolor=bg,
        border_radius=4,
        padding=ft.padding.symmetric(horizontal=4, vertical=3),
        content=ft.Row(cells, spacing=_COL_GAP),
    )


def _vet_term_row(term: str, count: int, on_vet, on_add_yaml, assign_coll: str) -> ft.Container:
    st = term_status(term)
    color = ft.Colors.GREEN_400 if st == "approved" else (ft.Colors.RED_300 if st == "rejected" else ft.Colors.GREY_300)
    return ft.Container(
        padding=ft.padding.symmetric(horizontal=2, vertical=1),
        content=ft.Row(
            [
                ft.Text(term, size=10, expand=True, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, color=color),
                ft.Text(f"{count:,}", size=10, color=ft.Colors.GREY_500, width=36, text_align=ft.TextAlign.RIGHT),
                ft.IconButton(icon=ft.Icons.CHECK, icon_size=14, tooltip="Approve tag", icon_color=ft.Colors.GREEN_400, on_click=lambda e, t=term: on_vet(t, "approved")),
                ft.IconButton(icon=ft.Icons.CLOSE, icon_size=14, tooltip="Reject tag", icon_color=ft.Colors.RED_300, on_click=lambda e, t=term: on_vet(t, "rejected")),
                ft.IconButton(icon=ft.Icons.ADD, icon_size=14, tooltip=f"Add to {assign_coll or 'YAML'}", disabled=not assign_coll, on_click=lambda e, t=term: on_add_yaml(t, assign_coll)),
            ],
            spacing=0,
        ),
    )


def _term_block(title: str, subtitle: str, items: list[tuple[str, int]], empty: str, *, height: int = 200, on_vet=None, on_add_yaml=None, assign_coll: str = "") -> ft.Container:
    body_h = height - 36
    if items:
        body = ft.Container(
            height=body_h,
            content=ft.Column(
                [_vet_term_row(t, c, on_vet, on_add_yaml, assign_coll) for t, c in items],
                spacing=0,
                tight=True,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
    else:
        body = ft.Container(height=body_h, content=ft.Text(empty, size=11, color=ft.Colors.GREY_500))
    return ft.Container(
        height=height,
        padding=6,
        bgcolor="#0b1220",
        border_radius=6,
        content=ft.Column(
            [
                ft.Text(title, size=10, weight=ft.FontWeight.BOLD),
                ft.Text(subtitle, size=9, color=ft.Colors.GREY_600),
                body,
            ],
            spacing=2,
            tight=True,
        ),
    )


def _stat_card(label: str, value: str, icon: str) -> ft.Container:
    return ft.Container(
        width=96,
        padding=6,
        bgcolor=_CARD_BG,
        border_radius=8,
        content=ft.Column(
            [
                ft.Row([ft.Icon(icon, size=13, color=ft.Colors.BLUE_300), ft.Text(label, size=9, color=ft.Colors.GREY_400)], spacing=3),
                ft.Text(value, size=14, weight=ft.FontWeight.BOLD),
            ],
            spacing=1,
            tight=True,
        ),
    )


def _guide_panel() -> ft.Container:
    steps = [
        ("1 Collect", "Pull likes + collections from Sketchfab → workbook (xlsx)."),
        ("2 Match", "Fill Suggested / Fuzzy columns from tags & names — no API writes."),
        ("3 Vet terms", "This tab — approve tags, + add to collections_terms.yaml."),
        ("4 Assign", "Liked tab — per-row Assign ▾ (suggestions first) or bulk Assign all filtered."),
        ("5 Auto-Assign", "Toolbar — apply YAML rules → Assigned column. Overwrite ON replaces existing Assigned."),
        ("6 Push", "Toolbar — sync Assigned to Sketchfab collections. Dry-run ON = preview only."),
    ]
    step_rows = [
        ft.Row(
            [
                ft.Text(label, size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.CYAN_300, width=108),
                ft.Text(text, size=10, color=ft.Colors.GREY_400, expand=True),
            ],
            spacing=8,
        )
        for label, text in steps
    ]
    cols = [
        ("Assigned", "Your target collection — Push adds the model here on Sketchfab."),
        ("Already In", "From API on Collect — model is already in these collections (read-only)."),
        ("Suggested / Fuzzy", "From Match — name/tag heuristics; shown first in Assign dropdown."),
        ("Auto-Assigned", "From Auto-Assign + YAML rules (respects Overwrite toggle)."),
        ("Manual", "Optional per-row override; Manual→Assigned copies Manual → Assigned."),
    ]
    col_rows = [
        ft.Row(
            [
                ft.Text(name, size=10, weight=ft.FontWeight.W_600, width=108, color=ft.Colors.AMBER_200),
                ft.Text(desc, size=10, color=ft.Colors.GREY_500, expand=True),
            ],
            spacing=8,
        )
        for name, desc in cols
    ]
    return ft.Container(
        padding=10,
        bgcolor=_PANEL_BG,
        border_radius=8,
        border=ft.border.all(1, "#334155"),
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.MENU_BOOK, size=18, color=ft.Colors.CYAN_300),
                        ft.Text("How assignment works", size=13, weight=ft.FontWeight.BOLD),
                    ],
                    spacing=8,
                ),
                ft.Text(
                    "Everything below is about sorting unassigned likes into collections. "
                    "Local changes stay in the workbook until you Push.",
                    size=10,
                    color=ft.Colors.GREY_400,
                ),
                ft.Container(height=4),
                ft.Text("Workflow", size=11, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_300),
                ft.Column(step_rows, spacing=3, tight=True),
                ft.Container(height=6),
                ft.Text("Workbook columns", size=11, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_300),
                ft.Column(col_rows, spacing=3, tight=True),
                ft.Container(height=6),
                ft.Text(
                    "Term lists (left) = tags/categories from unassigned models only. "
                    "✓ approve · ✗ reject · + add to YAML target collection. "
                    "Bulk candidates (right) = collections with many possible matches — filter Liked and assign in bulk.",
                    size=10,
                    color=ft.Colors.GREY_600,
                ),
            ],
            spacing=4,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
        ),
    )


class ReportTab(ft.Column):
    def __init__(self, on_find_similar, on_vet_term=None, on_open_collection=None, on_add_tag_yaml=None):
        super().__init__(expand=True, spacing=6)
        self._on_vet_term = on_vet_term
        self._on_open_collection = on_open_collection
        self._on_add_tag_yaml = on_add_tag_yaml
        self._colls_df = pd.DataFrame()
        self._last_liked = pd.DataFrame()
        self._last_pairs = None
        self._last_hide_nsfw = False
        self._last_hide_female = False
        self._last_hide_male = False
        self._term_filter = "unvetted"
        self._assign_coll_dd = ft.Dropdown(label="YAML target", width=160, dense=True, value="", options=[])
        self._term_filter_dd = ft.Dropdown(
            label="Show terms",
            width=130,
            dense=True,
            value="unvetted",
            options=[
                ft.dropdown.Option("all", "All"),
                ft.dropdown.Option("unvetted", "Unvetted"),
                ft.dropdown.Option("approved", "Approved"),
                ft.dropdown.Option("rejected", "Rejected"),
            ],
            on_change=lambda e: self._on_term_filter_change(),
        )
        self._guide = _guide_panel()
        self._stats_row = ft.Row(wrap=True, spacing=4)
        self._collections_list = ft.Column(spacing=1, scroll=ft.ScrollMode.AUTO, expand=True)
        self._terms_area = ft.Column(spacing=6, tight=True)
        self._bulk_list = ft.Column(spacing=1, scroll=ft.ScrollMode.AUTO, expand=True)
        self._collections_wrap = wrap_middle_drag_scroll(self._collections_list, expand=True)
        self._terms_wrap = wrap_middle_drag_scroll(self._terms_area, expand=True)
        self._bulk_wrap = wrap_middle_drag_scroll(self._bulk_list, expand=True)
        self._similar_list = ft.Column(spacing=2)

        self._left_panel = ft.Column(
            expand=True,
            spacing=4,
            controls=[
                ft.Row(
                    [
                        ft.Container(
                            width=168,
                            padding=6,
                            bgcolor=_PANEL_BG,
                            border_radius=8,
                            content=ft.Column(
                                [
                                    ft.Text("Collections", size=10, weight=ft.FontWeight.BOLD),
                                    ft.Container(expand=True, content=self._collections_wrap),
                                ],
                                expand=True,
                                spacing=2,
                            ),
                        ),
                        ft.Container(
                            expand=True,
                            padding=6,
                            bgcolor=_PANEL_BG,
                            border_radius=8,
                            content=ft.Column(
                                [
                                    ft.Row(
                                        [
                                    ft.Text("Top terms (unassigned models)", size=10, weight=ft.FontWeight.BOLD),
                                            ft.Container(expand=True),
                                            self._term_filter_dd,
                                            self._assign_coll_dd,
                                        ],
                                        spacing=6,
                                    ),
                                    ft.Text(
                                        "Pick YAML target collection, then ✓ approve · ✗ reject · + add term to rules file.",
                                        size=9,
                                        color=ft.Colors.GREY_600,
                                    ),
                                    self._terms_wrap,
                                ],
                                expand=True,
                                spacing=2,
                            ),
                        ),
                    ],
                    expand=True,
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                ft.Container(
                    padding=6,
                    bgcolor=_PANEL_BG,
                    border_radius=8,
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Text("Similar names", size=10, weight=ft.FontWeight.BOLD),
                                    ft.Container(expand=True),
                                    ft.IconButton(icon=ft.Icons.TRAVEL_EXPLORE, tooltip="Scan duplicates", icon_size=14, on_click=lambda e: on_find_similar()),
                                ],
                            ),
                            self._similar_list,
                        ],
                        spacing=2,
                        tight=True,
                    ),
                ),
            ],
        )

        self._bulk_panel = ft.Container(
            width=_BULK_WIDTH,
            padding=10,
            bgcolor=_PANEL_BG,
            border_radius=8,
            border=ft.border.all(1, "#334155"),
            content=ft.Column(
                [
                    ft.Text("Bulk assign candidates", size=13, weight=ft.FontWeight.BOLD),
                    ft.Text("Unassigned models that may belong in each collection.", size=11, color=ft.Colors.GREY_500),
                    ft.Text(
                        "Tags = author tag match · Title = name contains collection · "
                        "Sugg/Fuzzy = Match output · YAML = terms file · Strength = confidence",
                        size=10,
                        color=ft.Colors.GREY_600,
                    ),
                    _bulk_header_row(),
                    ft.Divider(height=1, color="#334155"),
                    self._bulk_wrap,
                    ft.Divider(height=1, color="#334155"),
                    ft.Text("Green high · Orange medium · Grey weak", size=10, color=ft.Colors.GREY_500),
                ],
                expand=True,
                spacing=4,
            ),
        )

        self.controls = [
            self._guide,
            self._stats_row,
            ft.Row(
                [ft.Container(expand=True, content=self._left_panel), self._bulk_panel],
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                spacing=8,
            ),
        ]

    def _on_term_filter_change(self) -> None:
        self._term_filter = self._term_filter_dd.value or "all"
        if not self._colls_df.empty:
            self.set_data(
                self._last_liked, self._colls_df, self._last_pairs,
                hide_nsfw=self._last_hide_nsfw, hide_female=self._last_hide_female, hide_male=self._last_hide_male,
            )

    def _vet(self, term: str, status: str) -> None:
        save_vet_term(term, status)
        if self._on_vet_term:
            self._on_vet_term(term, status)

    def _add_yaml(self, term: str, coll: str) -> None:
        if self._on_add_tag_yaml and coll:
            self._on_add_tag_yaml(term, coll)

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        """Re-apply N/W/M to Report mining / bulk / fuzzy without losing last workbook snapshot."""
        self.set_data(
            self._last_liked if self._last_liked is not None else pd.DataFrame(),
            self._colls_df if self._colls_df is not None else pd.DataFrame(),
            self._last_pairs,
            hide_nsfw=hide_nsfw,
            hide_female=hide_female,
            hide_male=hide_male,
        )

    def set_data(self, liked_df: pd.DataFrame, colls_df: pd.DataFrame, similar_pairs: list | None = None, *, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False):
        self._last_liked = liked_df
        self._last_pairs = similar_pairs
        self._last_hide_nsfw = hide_nsfw
        self._last_hide_female = hide_female
        self._last_hide_male = hide_male
        self._colls_df = colls_df if isinstance(colls_df, pd.DataFrame) else pd.DataFrame()
        coll_names = []
        if not self._colls_df.empty and "Collection Name" in self._colls_df.columns:
            coll_names = [str(n).strip() for n in self._colls_df["Collection Name"] if str(n).strip()]
        self._assign_coll_dd.options = [ft.dropdown.Option(c) for c in coll_names[:80]]
        if coll_names and not self._assign_coll_dd.value:
            self._assign_coll_dd.value = coll_names[0]
        assign_coll = self._assign_coll_dd.value or ""
        mode = self._term_filter_dd.value or self._term_filter or "unvetted"
        liked_df = liked_df if isinstance(liked_df, pd.DataFrame) else pd.DataFrame()
        colls_df = colls_df if isinstance(colls_df, pd.DataFrame) else pd.DataFrame()
        liked_df = filter_dataframe(liked_df, hide_nsfw=hide_nsfw, hide_female=hide_female, hide_male=hide_male)
        colls_df = filter_collections_df(colls_df, hide_nsfw=hide_nsfw, hide_female=hide_female, hide_male=hide_male)
        pending = unassigned_df(liked_df)

        assigned_n = in_coll_n = dl_yes = disk_yes = 0
        if not liked_df.empty:
            from report_stats import _nonempty
            a = liked_df.get("Assigned Collection(s)", pd.Series(index=liked_df.index, dtype=object))
            already = liked_df.get("Already In Collection(s)", pd.Series(index=liked_df.index, dtype=object))
            assigned_n = int(_nonempty(a).sum())
            in_coll_n = int(_nonempty(already).sum())
            if "Downloadable" in liked_df.columns:
                dl_yes = int((liked_df["Downloadable"].astype(str).str.lower() == "yes").sum())
            if "Downloaded" in liked_df.columns:
                disk_yes = int((liked_df["Downloaded"].astype(str).str.lower() == "yes").sum())

        self._stats_row.controls = [
            _stat_card("Liked", f"{len(liked_df):,}", ft.Icons.FAVORITE),
            _stat_card("Unassigned", f"{len(pending):,}", ft.Icons.HELP_OUTLINE),
            _stat_card("Assigned", f"{assigned_n:,}", ft.Icons.LABEL),
            _stat_card("In coll", f"{in_coll_n:,}", ft.Icons.BOOKMARK),
            _stat_card("Colls", f"{len(colls_df):,}", ft.Icons.FOLDER),
            _stat_card("DL", f"{dl_yes:,}", ft.Icons.DOWNLOAD),
            _stat_card("Disk", f"{disk_yes:,}", ft.Icons.SAVE_ALT),
        ]

        def _mine(fn, k):
            return filter_term_items(fn(pending, k), mode)

        self._terms_area.controls = [
            ft.Row(
                [
                    ft.Container(expand=True, content=_term_block("Author tags", "tags", _mine(mine_author_tags, 20), "none", height=210, on_vet=self._vet, on_add_yaml=self._add_yaml, assign_coll=assign_coll)),
                    ft.Container(expand=True, content=_term_block("Categories", "API", _mine(mine_categories, 16), "re-Collect", height=210, on_vet=self._vet, on_add_yaml=self._add_yaml, assign_coll=assign_coll)),
                    ft.Container(expand=True, content=_term_block("Name tokens", "titles", _mine(mine_name_tokens, 20), "none", height=210, on_vet=self._vet, on_add_yaml=self._add_yaml, assign_coll=assign_coll)),
                ],
                spacing=4,
                expand=True,
            ),
            ft.Row(
                [
                    ft.Container(expand=True, content=_term_block("Matcher", "suggested", _mine(mine_suggested_terms, 20), "run Match", height=210, on_vet=self._vet, on_add_yaml=self._add_yaml, assign_coll=assign_coll)),
                    ft.Container(expand=True, content=_term_block("Fuzzy hits", "near names", _mine(mine_fuzzy_hits, 20), "run Match", height=210, on_vet=self._vet, on_add_yaml=self._add_yaml, assign_coll=assign_coll)),
                ],
                spacing=4,
                expand=True,
            ),
        ]

        uid_by_name = {}
        slug_by_name = {}
        if "Collection Name" in colls_df.columns:
            for _, r in colls_df.iterrows():
                n = str(r.get("Collection Name", "")).strip()
                u = str(r.get("Collection UID", "")).strip()
                sl = str(r.get("Slug", "")).strip() if "Slug" in colls_df.columns else ""
                if n:
                    uid_by_name[n] = u
                    if sl:
                        slug_by_name[n] = sl

        self._collections_list.controls = []
        if colls_df is not None and not colls_df.empty and "Collection Name" in colls_df.columns:
            for name in colls_df["Collection Name"].astype(str):
                n = str(name).strip()
                if not n:
                    continue
                uid = uid_by_name.get(n, "")
                slug = slug_by_name.get(n, "")
                url = collection_public_url(n, uid, slug=slug) if uid else None
                self._collections_list.controls.append(
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=4, vertical=2),
                        content=ft.Row(
                            [
                                ft.TextButton(text=n, url=url, style=ft.ButtonStyle(padding=0)) if url else ft.Text(n, size=11),
                                ft.IconButton(
                                    icon=ft.Icons.SEARCH,
                                    icon_size=14,
                                    tooltip="Find in Collections tab",
                                    on_click=lambda e, nm=n: self._on_open_collection(nm) if self._on_open_collection else None,
                                ),
                            ],
                            spacing=0,
                        ),
                    )
                )
        else:
            self._collections_list.controls = [ft.Text("(Collect to load collections)", size=11, color=ft.Colors.GREY_500)]

        try:
            terms = Terms.from_yaml(_TERMS_PATH)
        except Exception:
            terms = None
        bulk = bulk_assignment_candidates(liked_df, colls_df, terms=terms, min_models=3)
        self._bulk_list.controls = []
        if bulk:
            for i, row in enumerate(bulk):
                conf = row["confidence"]
                color = ft.Colors.GREEN_400 if conf == "high" else (ft.Colors.ORANGE_400 if conf == "medium" else ft.Colors.GREY_500)
                bg = "#1a2332" if i % 2 else None
                self._bulk_list.controls.append(_bulk_data_row(row, color, bg, on_open_collection=self._on_open_collection))
        else:
            self._bulk_list.controls = [ft.Text("No candidates yet — run Match.", size=11, color=ft.Colors.GREY_500)]

        self._similar_list.controls = []
        if similar_pairs:
            for i, j, score in similar_pairs[:6]:
                if i < len(colls_df) and j < len(colls_df):
                    a = colls_df.iloc[i].get("Collection Name", "?")
                    b = colls_df.iloc[j].get("Collection Name", "?")
                    self._similar_list.controls.append(ft.Text(f"{score:.0f}%  {a}  /  {b}", size=11))
        if not self._similar_list.controls:
            self._similar_list.controls = [ft.Text("None flagged.", size=11, color=ft.Colors.GREY_500)]

        self.update()
