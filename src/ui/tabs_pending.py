"""Pending Push — review Assigned queue and cancel before Push."""
from __future__ import annotations

from datetime import datetime

import flet as ft

from content_filter import filter_dataframe
from push_assignments import pending_push_grouped
from ui.scroll_drag import wrap_middle_drag_scroll

_PANEL = "#0f172a"
_HDR = "#1e293b"
_ROW_BORDER = "#334155"
_ROW_ALT = "#1a2332"
_CHIP_BG = "#3f2d0a"
_CHIP_BORDER = "#a16207"


def _parse_assigned_at(raw: str) -> float:
    """Sort key for Assigned At stamps (newer = larger). Unknown → 0."""
    s = (raw or "").strip()
    if not s:
        return 0.0
    for fmt in (
        "%Y-%m-%d %H:%M UTC",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            cleaned = s.replace("Z", "+0000") if fmt.endswith("%z") else s
            if fmt.endswith("%z") and cleaned.endswith("+00:00"):
                cleaned = cleaned[:-6] + "+0000"
            return datetime.strptime(cleaned, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _fmt_assigned_when(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "—"
    # Compact display: drop trailing " UTC" for the column; keep full in tooltip.
    if s.endswith(" UTC"):
        return s[:-4]
    if "T" in s:
        return s.replace("T", " ")[:16]
    return s[:16]


class PendingPushTab(ft.Column):
    def __init__(self, page: ft.Page | None = None):
        super().__init__(expand=True, spacing=6)
        self._page = page
        self._liked_df = None
        self._on_cancel = None
        self._on_cancel_all = None
        self._on_push = None
        self._on_verify = None
        self._get_liked_df = None
        self._dirty = False
        self._sort_recent_first = True
        self._hide_nsfw = True
        self._hide_female = True
        self._hide_male = True
        self._row_by_uid: dict[str, ft.Container] = {}
        self._chips_by_uid: dict[str, ft.Row] = {}
        self._status = ft.Text("No pending assignments.", size=12, color=ft.Colors.GREY_400)
        self._sync = ft.Text("", size=11, color=ft.Colors.GREY_500)
        self._list = ft.ListView(expand=True, spacing=0, padding=0)
        self._list_wrap = wrap_middle_drag_scroll(self._list, expand=True)
        self._when_hdr = ft.TextButton(
            content=ft.Text("Assigned ▾", weight=ft.FontWeight.BOLD, size=11, color=ft.Colors.AMBER_200),
            tooltip="Sort by when assigned (click to toggle)",
            style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=4, vertical=0)),
            on_click=lambda e: self._toggle_sort(),
        )
        self._cancel_all_btn = ft.OutlinedButton(
            "Cancel all",
            icon=ft.Icons.DELETE_SWEEP,
            tooltip="Clear every pending assignment from the queue (not pushed to Sketchfab)",
            on_click=lambda e: self._confirm_cancel_all(),
        )

        self.controls = [
            ft.Row(
                [
                    ft.Text("Pending Push", size=14, weight=ft.FontWeight.BOLD),
                    self._status,
                    ft.Container(expand=True),
                    ft.TextButton(
                        "Check push sync",
                        icon=ft.Icons.SYNC_PROBLEM,
                        tooltip=(
                            "Compare local Pending queue with Sketchfab (dry-run). "
                            "Shows how many still need posting vs already on the site, "
                            "plus Collect/Push freshness."
                        ),
                        on_click=lambda e: self._on_verify() if self._on_verify else None,
                    ),
                    ft.TextButton("Refresh", icon=ft.Icons.REFRESH, on_click=lambda e: self.refresh()),
                    self._cancel_all_btn,
                    ft.FilledButton(
                        "Push now",
                        icon=ft.Icons.UPLOAD,
                        on_click=lambda e: self._on_push() if self._on_push else None,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            self._sync,
            ft.Text(
                "Pending = Assigned collection not yet in Already In (workbook). "
                "Unpushed in the header counts those models. "
                "Check push sync dry-runs against Sketchfab to see what still needs posting vs already on the site. "
                "Cancel (×) drops just that collection before you Push. Cancel all clears the whole queue.",
                size=11,
                color=ft.Colors.GREY_500,
            ),
            ft.Container(
                expand=True,
                bgcolor=_PANEL,
                border_radius=8,
                content=ft.Column(
                    [
                        ft.Container(
                            bgcolor=_HDR,
                            padding=ft.padding.symmetric(horizontal=10, vertical=8),
                            content=ft.Row(
                                [
                                    ft.Container(width=48, content=ft.Text("", size=11)),
                                    ft.Container(expand=3, content=ft.Text("Model", weight=ft.FontWeight.BOLD, size=11)),
                                    ft.Container(
                                        expand=4,
                                        content=ft.Text("→ Pending collections", weight=ft.FontWeight.BOLD, size=11),
                                    ),
                                    ft.Container(width=130, content=self._when_hdr),
                                ],
                                spacing=8,
                            ),
                        ),
                        self._list_wrap,
                    ],
                    spacing=0,
                    expand=True,
                ),
            ),
        ]

    def set_callbacks(
        self,
        on_cancel,
        on_push=None,
        get_liked_df=None,
        on_verify=None,
        on_cancel_all=None,
    ) -> None:
        self._on_cancel = on_cancel
        self._on_push = on_push
        self._get_liked_df = get_liked_df
        if on_verify is not None:
            self._on_verify = on_verify
        self._on_cancel_all = on_cancel_all

    def _confirm_cancel_all(self) -> None:
        if not self._on_cancel_all:
            return
        page = self.page or self._page
        if not page:
            self._on_cancel_all()
            return

        def _close(_e=None):
            try:
                page.close(dlg)
            except Exception:
                dlg.open = False
                try:
                    page.update()
                except Exception:
                    pass

        def _yes(_e=None):
            _close()
            self._on_cancel_all()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Cancel all pending?"),
            content=ft.Text(
                "Clears every Assigned collection that is still waiting to Push. "
                "Nothing is removed from Sketchfab — only the local queue."
            ),
            actions=[
                ft.TextButton("Keep queue", on_click=_close),
                ft.TextButton("Cancel all", on_click=_yes, style=ft.ButtonStyle(color=ft.Colors.RED_300)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        try:
            page.open(dlg)
        except Exception:
            # Older open path
            page.dialog = dlg
            dlg.open = True
            page.update()

    def scroll_to_top(self) -> None:
        try:
            self._list.scroll_to(offset=0, duration=200)
        except Exception:
            pass

    def set_df(self, liked_df) -> None:
        self._liked_df = liked_df
        self._dirty = False
        self.refresh()

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        """View-only — pending rows stay in workbook / still Push; N/W/M just hides them here."""
        self._hide_nsfw = bool(hide_nsfw)
        self._hide_female = bool(hide_female)
        self._hide_male = bool(hide_male)
        # Caller usually follows with set_df/refresh; only paint if we already have data mounted.
        if self._liked_df is not None and (self.page or self._page):
            self.refresh()

    def mark_dirty(self, liked_df=None) -> None:
        """Queue refresh when the Pending tab is opened (avoids Liked scroll yank)."""
        if liked_df is not None:
            self._liked_df = liked_df
        self._dirty = True

    def _toggle_sort(self) -> None:
        self._sort_recent_first = not self._sort_recent_first
        self.refresh()

    def _paint_when_hdr(self) -> None:
        arrow = "▾" if self._sort_recent_first else "▴"
        label = f"Assigned {arrow}"
        tip = (
            "Newest first — click for oldest first"
            if self._sort_recent_first
            else "Oldest first — click for newest first"
        )
        txt = self._when_hdr.content
        if isinstance(txt, ft.Text):
            txt.value = label
        self._when_hdr.tooltip = tip

    def remove_collection_chip(self, uid: str, collection: str) -> bool:
        """Drop one pending chip in place — no full list rebuild. Returns True if handled."""
        uid = str(uid or "").strip()
        collection = str(collection or "").strip()
        chips_row = self._chips_by_uid.get(uid)
        row = self._row_by_uid.get(uid)
        if chips_row is None or row is None:
            return False
        keep: list[ft.Control] = []
        removed = False
        for c in list(chips_row.controls or []):
            # Chip container → Row → [Text(coll), IconButton]
            try:
                inner = getattr(c, "content", None)
                kids = getattr(inner, "controls", None) or []
                label = kids[0] if kids else None
                name = str(getattr(label, "value", "") or "")
            except Exception:
                name = ""
            if name.casefold() == collection.casefold():
                removed = True
                continue
            keep.append(c)
        if not removed:
            return False
        if not keep:
            # Last collection — remove the whole model row.
            try:
                self._list.controls = [x for x in (self._list.controls or []) if x is not row]
            except Exception:
                pass
            self._row_by_uid.pop(uid, None)
            self._chips_by_uid.pop(uid, None)
        else:
            chips_row.controls = keep
        models = len(self._row_by_uid)
        pairs = sum(len(getattr(r, "controls", None) or []) for r in self._chips_by_uid.values())
        if not models:
            self._status.value = "No pending assignments — nothing to Push."
        elif pairs == models:
            self._status.value = f"{models:,} pending"
        else:
            self._status.value = f"{models:,} models · {pairs:,} assignments pending"
        page = self.page or self._page
        if page:
            try:
                self.update()
            except Exception:
                try:
                    page.update()
                except Exception:
                    pass
        return True

    def refresh_if_dirty(self) -> None:
        if self._dirty:
            self._dirty = False
            self.refresh()

    def set_sync_info(self, text: str, *, warn: bool = False) -> None:
        self._sync.value = text or ""
        self._sync.color = ft.Colors.AMBER_300 if warn else ft.Colors.GREY_500
        if self.page or self._page:
            try:
                self._sync.update()
            except Exception:
                pass

    def _coll_chip(self, uid: str, coll: str) -> ft.Control:
        return ft.Container(
            bgcolor=_CHIP_BG,
            border=ft.border.all(1, _CHIP_BORDER),
            border_radius=12,
            padding=ft.padding.only(left=10, right=2, top=1, bottom=1),
            content=ft.Row(
                [
                    ft.Text(coll, size=11, color=ft.Colors.AMBER_200, tooltip=coll),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE,
                        icon_size=13,
                        icon_color=ft.Colors.RED_200,
                        tooltip=f"Cancel → {coll}",
                        style=ft.ButtonStyle(padding=2),
                        on_click=lambda e, u=uid, c=coll: (self._on_cancel(u, c) if self._on_cancel else None),
                    ),
                ],
                spacing=0,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def refresh(self) -> None:
        # Always pull live Liked frame — assigns/Push replace state.liked_df and
        # a stale snapshot made Refresh look like a no-op.
        if self._get_liked_df is not None:
            try:
                self._liked_df = self._get_liked_df()
            except Exception:
                pass
        self._dirty = False
        self._row_by_uid.clear()
        self._chips_by_uid.clear()
        src = self._liked_df
        full_groups = pending_push_grouped(src)
        view_df = filter_dataframe(
            src,
            hide_nsfw=self._hide_nsfw,
            hide_female=self._hide_female,
            hide_male=self._hide_male,
        ) if src is not None else src
        groups = pending_push_grouped(view_df)
        groups.sort(
            key=lambda g: _parse_assigned_at(str(g.get("assigned_at") or "")),
            reverse=self._sort_recent_first,
        )
        self._paint_when_hdr()
        models = len(groups)
        pairs = sum(len(g["collections"]) for g in groups)
        hidden = max(0, len(full_groups) - models)
        if not full_groups:
            self._status.value = "No pending assignments — nothing to Push."
        elif not models and hidden:
            self._status.value = f"N/W/M hiding all {hidden:,} pending — turn filters off to review"
        elif pairs == models:
            self._status.value = f"{models:,} pending" + (f" · N/W/M hiding {hidden:,}" if hidden else "")
        else:
            self._status.value = (
                f"{models:,} models · {pairs:,} assignments pending"
                + (f" · N/W/M hiding {hidden:,}" if hidden else "")
            )
        controls: list[ft.Control] = []
        if not groups:
            controls.append(
                ft.Container(
                    padding=24,
                    content=ft.Text(
                        "Assign via chips / N·W·M / dropdown on Liked — they show up here until Push.",
                        size=12,
                        color=ft.Colors.GREY_500,
                    ),
                )
            )
        else:
            for i, g in enumerate(groups):
                thumb = g.get("thumb") or ""
                img = (
                    ft.Image(src=thumb, width=40, height=40, fit=ft.ImageFit.COVER, border_radius=4)
                    if thumb
                    else ft.Container(width=40, height=40, bgcolor="#334155", border_radius=4)
                )
                chips = ft.Row(
                    [self._coll_chip(g["uid"], c) for c in g["collections"]],
                    spacing=6,
                    wrap=True,
                    run_spacing=6,
                )
                when_raw = str(g.get("assigned_at") or "").strip()
                when_txt = _fmt_assigned_when(when_raw)
                row = ft.Container(
                    bgcolor=_ROW_ALT if i % 2 else None,
                    border=ft.border.only(bottom=ft.BorderSide(1, _ROW_BORDER)),
                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                    data=g["uid"],
                    content=ft.Row(
                        [
                            ft.Container(width=48, content=img),
                            ft.Container(
                                expand=3,
                                content=ft.Column(
                                    [
                                        ft.Text(
                                            g["name"] or g["uid"],
                                            size=12,
                                            weight=ft.FontWeight.W_500,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                        ),
                                        ft.Text(
                                            g.get("author") or g["uid"],
                                            size=10,
                                            color=ft.Colors.GREY_500,
                                            max_lines=1,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                        ),
                                    ],
                                    spacing=1,
                                    tight=True,
                                ),
                            ),
                            ft.Container(expand=4, content=chips),
                            ft.Container(
                                width=130,
                                content=ft.Text(
                                    when_txt,
                                    size=11,
                                    color=ft.Colors.GREY_400 if when_txt == "—" else ft.Colors.AMBER_100,
                                    tooltip=when_raw or "No Assigned At stamp (assigned before this build)",
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
                self._row_by_uid[g["uid"]] = row
                self._chips_by_uid[g["uid"]] = chips
                controls.append(row)
        self._list.controls = controls
        page = self.page or self._page
        if page:
            try:
                self.update()
            except Exception:
                try:
                    page.update()
                except Exception:
                    pass
