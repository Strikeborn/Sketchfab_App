from __future__ import annotations

import flet as ft

from changelog_data import load_changelog
from ui.scroll_drag import wrap_middle_drag_scroll
from version import APP_NAME, APP_VERSION

_PANEL = "#0f172a"
_CARD = "#0b1220"
_BORDER = "#334155"

_TYPE_COLORS = {
    "feature": ft.Colors.BLUE_400,
    "fix": ft.Colors.ORANGE_400,
    "breaking": ft.Colors.RED_400,
}

_ICON_MAP = {
    "favorite": ft.Icons.FAVORITE,
    "travel_explore": ft.Icons.TRAVEL_EXPLORE,
    "folder": ft.Icons.FOLDER,
    "bookmarks": ft.Icons.BOOKMARKS,
    "analytics": ft.Icons.ANALYTICS,
    "history": ft.Icons.HISTORY,
    "download": ft.Icons.DOWNLOAD,
    "manage_search": ft.Icons.MANAGE_SEARCH,
    "rule": ft.Icons.RULE,
    "folder_shared": ft.Icons.FOLDER_SHARED,
    "upload": ft.Icons.UPLOAD,
}


def _arrow() -> ft.Row:
    return ft.Row(
        [
            ft.Container(width=10, height=2, bgcolor="#475569"),
            ft.Icon(ft.Icons.ARROW_FORWARD, size=14, color="#64748b"),
            ft.Container(width=10, height=2, bgcolor="#475569"),
        ],
        spacing=0,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def _node(label: str, icon, color: str, *, sub: str = "") -> ft.Container:
    return ft.Container(
        width=120,
        height=96,
        padding=8,
        bgcolor="#111827",
        border_radius=12,
        border=ft.border.all(2, color),
        content=ft.Column(
            [
                ft.Icon(icon, size=30, color=color),
                ft.Text(label, size=11, weight=ft.FontWeight.W_600, text_align=ft.TextAlign.CENTER, max_lines=2),
                ft.Text(sub, size=9, color=ft.Colors.GREY_600, text_align=ft.TextAlign.CENTER) if sub else ft.Container(),
            ],
            spacing=4,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        ),
    )


def _flow_row(nodes: list[ft.Control]) -> ft.Row:
    parts: list[ft.Control] = []
    for i, node in enumerate(nodes):
        if i:
            parts.append(_arrow())
        parts.append(node)
    return ft.Row(parts, spacing=4, wrap=False, scroll=ft.ScrollMode.AUTO)


def _goal_row(label: str, detail: str, *, done: bool = False) -> ft.Container:
    icon = ft.Icons.CHECK_CIRCLE if done else ft.Icons.FLAG
    color = ft.Colors.GREEN_400 if done else ft.Colors.AMBER_300
    return ft.Container(
        padding=ft.padding.symmetric(horizontal=10, vertical=8),
        bgcolor=_CARD,
        border_radius=8,
        border=ft.border.all(1, "#475569"),
        content=ft.Row(
            [
                ft.Icon(icon, size=18, color=color),
                ft.Column(
                    [
                        ft.Text(label, size=12, weight=ft.FontWeight.W_600),
                        ft.Text(detail, size=10, color=ft.Colors.GREY_500) if detail else ft.Container(),
                    ],
                    spacing=2,
                    expand=True,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
        ),
    )


def _upcoming_row(label: str, detail: str, priority: str = "") -> ft.Container:
    badge = None
    pri = (priority or "").strip().lower()
    if pri == "high":
        badge = ft.Container(
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            bgcolor="#7f1d1d",
            border_radius=4,
            content=ft.Text("high", size=8, color=ft.Colors.RED_200),
        )
    elif pri in ("next", "soon"):
        badge = ft.Container(
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            bgcolor="#1e3a5f",
            border_radius=4,
            content=ft.Text(pri, size=8, color=ft.Colors.CYAN_200),
        )
    title_row: list[ft.Control] = [ft.Text(label, size=12, weight=ft.FontWeight.W_600)]
    if badge:
        title_row.append(badge)
    return ft.Container(
        padding=ft.padding.symmetric(horizontal=10, vertical=8),
        bgcolor=_CARD,
        border_radius=8,
        border=ft.border.only(left=ft.BorderSide(2, ft.Colors.CYAN_400)),
        content=ft.Column(
            [
                ft.Row(title_row, spacing=8),
                ft.Text(detail, size=10, color=ft.Colors.GREY_500) if detail else ft.Container(),
            ],
            spacing=4,
            tight=True,
        ),
    )


def _build_goals_upcoming(app: dict) -> ft.Row:
    goals = app.get("goals") or {}
    upcoming = app.get("upcoming") or {}

    goal_items = goals.get("items") or []
    goal_rows = [
        _goal_row(
            str(g.get("label") or ""),
            str(g.get("detail") or ""),
            done=bool(g.get("done")),
        )
        for g in goal_items
    ]
    goals_col = ft.Column(
        [
            ft.Row(
                [
                    ft.Icon(ft.Icons.FLAG, size=16, color=ft.Colors.AMBER_300),
                    ft.Text("Goals", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_200),
                ],
                spacing=6,
            ),
            ft.Text(str(goals.get("headline") or ""), size=11, color=ft.Colors.GREY_400) if goals.get("headline") else ft.Container(),
            ft.Column(goal_rows, spacing=6) if goal_rows else ft.Text("(add goals in changelog.yaml)", size=10, color=ft.Colors.GREY_600),
        ],
        spacing=8,
        expand=True,
    )

    up_items = upcoming.get("items") or []
    up_rows = [
        _upcoming_row(
            str(u.get("label") or ""),
            str(u.get("detail") or ""),
            str(u.get("priority") or ""),
        )
        for u in up_items
    ]
    upcoming_col = ft.Column(
        [
            ft.Row(
                [
                    ft.Icon(ft.Icons.ROCKET_LAUNCH, size=16, color=ft.Colors.CYAN_300),
                    ft.Text("Upcoming", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_200),
                ],
                spacing=6,
            ),
            ft.Text(str(upcoming.get("headline") or ""), size=11, color=ft.Colors.GREY_400) if upcoming.get("headline") else ft.Container(),
            ft.Column(up_rows, spacing=6) if up_rows else ft.Text("(add upcoming in changelog.yaml)", size=10, color=ft.Colors.GREY_600),
        ],
        spacing=8,
        expand=True,
    )

    return ft.Row([goals_col, upcoming_col], spacing=16, expand=True, vertical_alignment=ft.CrossAxisAlignment.START)


def _todo_card(step: int, label: str, detail: str, icon_key: str, color: str) -> ft.Container:
    icon = _ICON_MAP.get(icon_key, ft.Icons.CHECK_CIRCLE_OUTLINE)
    return ft.Container(
        width=148,
        height=118,
        padding=10,
        bgcolor=_CARD,
        border_radius=10,
        border=ft.border.all(1, color),
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Container(
                            width=22,
                            height=22,
                            border_radius=11,
                            bgcolor=color,
                            alignment=ft.alignment.center,
                            content=ft.Text(str(step), size=11, weight=ft.FontWeight.BOLD, color="#0f172a"),
                        ),
                        ft.Icon(icon, size=18, color=color),
                    ],
                    spacing=6,
                ),
                ft.Text(label, size=12, weight=ft.FontWeight.BOLD, max_lines=1),
                ft.Text(detail, size=9, color=ft.Colors.GREY_500, max_lines=3),
            ],
            spacing=4,
            tight=True,
        ),
    )


def _build_direction_panel(app: dict) -> ft.Container:
    d = app.get("direction") or {}
    headline = d.get("headline") or "Manage liked models and collections"
    summary = d.get("summary") or ""
    flow = d.get("flow") or "Collect → Match → Assign → Push"
    detail = d.get("detail") or ""
    parts = [p.strip() for p in flow.split("→") if p.strip()]
    flow_nodes = []
    for i, part in enumerate(parts):
        if i:
            flow_nodes.append(_arrow())
        flow_nodes.append(
            ft.Container(
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                bgcolor="#111827",
                border_radius=8,
                border=ft.border.all(1, "#475569"),
                content=ft.Text(part, size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_200),
            )
        )
    return ft.Container(
        padding=12,
        bgcolor=_PANEL,
        border_radius=8,
        border=ft.border.all(1, _BORDER),
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.EXPLORE, size=20, color=ft.Colors.CYAN_300),
                        ft.Text("Direction", size=14, weight=ft.FontWeight.BOLD),
                    ],
                    spacing=8,
                ),
                ft.Text(headline, size=13, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_100),
                ft.Text(summary, size=11, color=ft.Colors.GREY_400) if summary else ft.Container(),
                ft.Row(flow_nodes, spacing=4, scroll=ft.ScrollMode.AUTO, wrap=False),
                ft.Text(detail, size=10, color=ft.Colors.GREY_500) if detail else ft.Container(),
            ],
            spacing=8,
            tight=True,
        ),
    )


def _build_todos_panel(app: dict) -> ft.Container:
    items = app.get("things_to_do") or []
    cards = []
    for item in items:
        cards.append(
            _todo_card(
                int(item.get("step") or len(cards) + 1),
                str(item.get("label") or ""),
                str(item.get("detail") or ""),
                str(item.get("icon") or ""),
                str(item.get("color") or "#64748b"),
            )
        )
    body = ft.Row(cards, spacing=8, scroll=ft.ScrollMode.AUTO, wrap=False) if cards else ft.Text("(add things_to_do in changelog.yaml)", size=11, color=ft.Colors.GREY_500)
    return ft.Container(
        padding=12,
        bgcolor=_PANEL,
        border_radius=8,
        border=ft.border.all(1, _BORDER),
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.CHECKLIST, size=20, color=ft.Colors.GREEN_300),
                        ft.Text("Things to do", size=14, weight=ft.FontWeight.BOLD),
                    ],
                    spacing=8,
                ),
                body,
            ],
            spacing=8,
            tight=True,
        ),
    )


def _version_line(entry: dict) -> ft.Container:
    ver = entry.get("version", "?")
    date = entry.get("date", "")
    title = entry.get("title", "")
    kind = entry.get("type", "feature")
    color = _TYPE_COLORS.get(kind, ft.Colors.GREY_500)
    items = entry.get("items") or entry.get("highlights") or []
    summary = " · ".join(str(i) for i in items[:2])
    if len(items) > 2:
        summary += f" · +{len(items) - 2} more"
    detail_bits: list[ft.Control] = []
    if summary:
        detail_bits.append(ft.Text(summary, size=10, color=ft.Colors.GREY_500))
    works = entry.get("works") or []
    doesnt = entry.get("does_not") or entry.get("doesnt") or []
    fixes = entry.get("fixes") or []
    if works:
        detail_bits.append(
            ft.Text("Works: " + " · ".join(str(w) for w in works[:3]), size=9, color=ft.Colors.GREEN_300)
        )
    if doesnt:
        detail_bits.append(
            ft.Text("Doesn’t: " + " · ".join(str(w) for w in doesnt[:3]), size=9, color=ft.Colors.AMBER_200)
        )
    if fixes:
        detail_bits.append(
            ft.Text("Fixed: " + " · ".join(str(w) for w in fixes[:3]), size=9, color=ft.Colors.CYAN_200)
        )
    return ft.Container(
        padding=ft.padding.symmetric(horizontal=8, vertical=5),
        bgcolor=_CARD,
        border_radius=4,
        border=ft.border.only(left=ft.BorderSide(2, color)),
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(f"v{ver}", size=10, weight=ft.FontWeight.BOLD, color=color, width=44),
                        ft.Text(title, size=11, weight=ft.FontWeight.W_600, expand=True),
                        ft.Text(date, size=9, color=ft.Colors.GREY_600),
                    ],
                    spacing=6,
                ),
                *detail_bits,
            ],
            spacing=2,
            tight=True,
        ),
    )


def _build_system_map(app: dict) -> ft.Column:
    api = _node("Sketchfab", ft.Icons.CLOUD, "#38bdf8")
    collect = _node("Collect", ft.Icons.DOWNLOAD, "#22d3ee", sub="API pull")
    workbook = _node("Workbook", ft.Icons.TABLE_CHART, "#a78bfa", sub="xlsx")
    match = _node("Match", ft.Icons.MANAGE_SEARCH, "#f472b6", sub="suggest")
    terms = _node("Terms", ft.Icons.RULE, "#fb923c", sub="yaml")
    assign = _node("Auto-Assign", ft.Icons.LABEL, "#34d399")
    push = _node("Push", ft.Icons.UPLOAD, "#facc15", sub="sync")
    scan = _node("Scan DL", ft.Icons.FOLDER_OPEN, "#94a3b8", sub="disk")
    ui = _node("UI tabs", ft.Icons.DASHBOARD, "#64748b")

    ingest = _flow_row([api, collect, workbook, match])
    rules = _flow_row([terms, assign, workbook])
    outbound = _flow_row([workbook, push, api])
    local = _flow_row([scan, workbook, ui])

    return ft.Column(
        [
            ft.Text("Data flow", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_200),
            ingest,
            ft.Container(height=16),
            ft.Text("Assignment", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_200),
            rules,
            ft.Container(height=16),
            ft.Text("Sync & local", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_200),
            ft.Column([outbound, ft.Container(height=10), local], spacing=8, tight=True),
            ft.Container(height=20),
            _build_goals_upcoming(app),
        ],
        spacing=8,
        expand=True,
        alignment=ft.MainAxisAlignment.START,
    )


class ChangelogTab(ft.Column):
    def __init__(self):
        super().__init__(expand=True, spacing=6)
        self._version_badge = ft.Text("", size=18, weight=ft.FontWeight.BOLD)
        self._tagline = ft.Text("", size=11, color=ft.Colors.GREY_400)
        self._direction_panel = ft.Container()
        self._todos_panel = ft.Container()
        self._map_col = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)
        self._history_col = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)
        self._map_scroll = wrap_middle_drag_scroll(self._map_col, expand=True)
        self._history_scroll = wrap_middle_drag_scroll(self._history_col, expand=True)

        self.controls = [
            ft.Container(
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                bgcolor=_PANEL,
                border_radius=8,
                content=ft.Row(
                    [
                        ft.Column([self._version_badge, self._tagline], spacing=2, expand=True),
                        ft.Text("data/changelog.yaml", size=10, color=ft.Colors.CYAN_300, font_family="Consolas"),
                    ],
                ),
            ),
            self._direction_panel,
            self._todos_panel,
            ft.Row(
                [
                    ft.Container(
                        expand=True,
                        padding=8,
                        bgcolor=_PANEL,
                        border_radius=8,
                        border=ft.border.all(1, _BORDER),
                        content=ft.Column(
                            [
                                ft.Row(
                                    [ft.Icon(ft.Icons.HUB, size=14, color=ft.Colors.GREY_400), ft.Text("Systems", size=12, weight=ft.FontWeight.BOLD)],
                                    spacing=6,
                                ),
                                ft.Container(expand=True, content=self._map_scroll),
                            ],
                            expand=True,
                            spacing=6,
                        ),
                    ),
                    ft.Container(
                        width=300,
                        padding=8,
                        bgcolor=_PANEL,
                        border_radius=8,
                        border=ft.border.all(1, _BORDER),
                        content=ft.Column(
                            [
                                ft.Row(
                                    [ft.Icon(ft.Icons.HISTORY, size=14, color=ft.Colors.GREY_400), ft.Text("Releases", size=12, weight=ft.FontWeight.BOLD)],
                                    spacing=6,
                                ),
                                ft.Container(expand=True, content=self._history_scroll),
                            ],
                            expand=True,
                            spacing=4,
                        ),
                    ),
                ],
                expand=True,
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        ]
        self.refresh()

    def refresh(self) -> None:
        data = load_changelog()
        app = data.get("app") or {}
        ver = data.get("version") or APP_VERSION
        started = app.get("started", "")
        tag = app.get("tagline") or ""
        self._version_badge.value = f"{APP_NAME}  v{ver}"
        self._tagline.value = tag + (f"  ·  {started}" if started else "")

        self._direction_panel = _build_direction_panel(app)
        self._todos_panel = _build_todos_panel(app)
        self.controls[1] = self._direction_panel
        self.controls[2] = self._todos_panel

        self._map_col.controls = [
            ft.Container(
                expand=True,
                alignment=ft.alignment.center,
                padding=20,
                content=_build_system_map(app),
            )
        ]
        self._history_col.controls = [_version_line(e) for e in (data.get("entries") or [])]

        if self.page:
            self.update()
