"""Liked tab style constants + pure row/status helpers (extracted from tabs_liked)."""
from __future__ import annotations

import flet as ft
import pandas as pd

_PANEL = "#0f172a"
_HDR_BG = "#1e293b"
_HDR_BORDER = "#475569"
_ROW_BORDER = "#334155"
_ROW_ALT = "#1a2332"
_CHIP_BG = "#1e3a5f"
_CHIP_HOVER = "#254a75"
_MIN_COLS = 2
_MAX_COLS = 10
_DEFAULT_COLS = 4
_MIN_CARD_W = 152
_CHIP_CAP = 6
_CHIP_H = 18
_CHIP_H_DENSE = 14
_CHIP_ZONE_H = 20  # list view chips
_ACTIONS_ZONE_H = 0
_SCROLL_LOAD_THRESHOLD = 280
_LICENSE_COLOR = "#fef08a"  # yellow-200 — high contrast on dark scrims
# Window chrome to the right of the grid (Activity pane ~280 + padding).
_GRID_SIDE_CHROME = 340
_GRID_GAP = 8
# Before maximize/layout settles, page.width is often a tiny default → postage-stamp cards.
_FALLBACK_GRID_AVAIL = 1100

# Card status chrome (Already In / pending Push)
_STATUS_PENDING = "#f59e0b"
_STATUS_IN = "#34d399"
_STATUS_UNLISTED = "#94a3b8"
_BG_GRID = "#111827"
_BG_PENDING = "#1c1910"
_BG_IN = "#0f1a14"
_BG_PENDING_IN = "#151a10"
# Dark status scrims — Flet opacity is "#rrggbb,0.92" (8-digit hex is NOT reliable)
_STATUS_PENDING_SCRIM = ft.Colors.with_opacity(0.92, "#78350f")  # amber-900
_STATUS_IN_SCRIM = ft.Colors.with_opacity(0.92, "#064e3b")  # emerald-950
_STATUS_NEUTRAL_SCRIM = ft.Colors.with_opacity(0.94, "#020617")  # near-black
_RAIL_W = 40
_META_ON_SCRIM = "#ffffff"
_LICENSE_ON_SCRIM = "#fef08a"

_LIST_COLS = [
    "Thumbnail", "Name", "Author", "Categories", "Tags", "License", "Face Count", "Likes",
    "Downloadable", "Downloaded", "Assigned Collection(s)", "Assign",
]
_COL_WIDTHS = {
    "Thumbnail": 56,
    "Name": 160,
    "Author": 90,
    "Categories": 95,
    "Tags": 100,
    "License": 130,
    "Face Count": 52,
    "Likes": 52,
    "Downloadable": 30,
    "Downloaded": 30,
    "Assigned Collection(s)": 110,
    "Assign": 220,
}
_ACTIONS_W = 88
_SEL_COL_W = 30
_HDR_LABELS = {
    "Face Count": "Faces",
    "Likes": "Likes",
    "Downloadable": "DL",
    "Downloaded": "Disk",
    "Assigned Collection(s)": "Assigned",
    "Assign": "Tags / Assign",
}
_COL_SPACING = 10
_ROW_BATCH = 6


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"none", "nan", "<na>"} else s


def _uid(row: pd.Series) -> str:
    return _fmt(row.get("UID") or row.get("Model UID"))


def _name(row: pd.Series) -> str:
    return _fmt(row.get("Name") or row.get("Model Name"))


def _url(row: pd.Series) -> str | None:
    u = _uid(row)
    return f"https://sketchfab.com/3d-models/{u}" if u else None


def _yes_icon(val: object) -> ft.Icon:
    ok = str(val).strip().lower() in {"1", "true", "yes", "y"}
    return ft.Icon(
        ft.Icons.CHECK_CIRCLE if ok else ft.Icons.CANCEL,
        color=ft.Colors.GREEN_400 if ok else ft.Colors.GREY_600,
        size=16,
    )


def _csv_names(cell) -> list[str]:
    return [p.strip() for p in _fmt(cell).split(",") if p.strip()]


def _row_collection_status(
    row: pd.Series,
    pending_override: str | None = None,
) -> tuple[bool, bool, str, str, bool]:
    """
    Returns (is_pending_push, is_already_in, pending_label, already_label, is_unlisted).
    Pending = Assigned name not yet in Already In.
    already_label lists multiple collections when present.
    """
    assigned = _csv_names(pending_override) if pending_override else _csv_names(row.get("Assigned Collection(s)"))
    already = _csv_names(row.get("Already In Collection(s)"))
    already_cf = {a.casefold() for a in already}
    is_unlisted = "unlisted" in already_cf
    pending_names = [a for a in assigned if a.casefold() not in already_cf]
    is_pending = bool(pending_names)
    is_in = bool(already)
    pend_lbl = pending_names[0] if pending_names else ""
    if not already:
        already_lbl = ""
    elif is_unlisted:
        already_lbl = "Unlisted"
    elif len(already) == 1:
        already_lbl = already[0]
    elif len(already) == 2:
        already_lbl = f"{already[0]}, {already[1]}"
    else:
        already_lbl = f"{already[0]}, {already[1]} +{len(already) - 2}"
    return is_pending, is_in, pend_lbl, already_lbl, is_unlisted


def _already_tooltip(row: pd.Series) -> str | None:
    names = _csv_names(row.get("Already In Collection(s)"))
    if len(names) <= 1:
        return None
    return "Already in:\n" + "\n".join(f"• {n}" for n in names)


def _status_border(is_pending: bool, is_in: bool):
    if is_pending and is_in:
        return ft.border.only(
            left=ft.BorderSide(4, _STATUS_PENDING),
            bottom=ft.BorderSide(2, _STATUS_IN),
        )
    if is_pending:
        return ft.border.only(left=ft.BorderSide(4, _STATUS_PENDING))
    if is_in:
        return ft.border.only(left=ft.BorderSide(4, _STATUS_IN))
    return None


def _status_tint60(is_pending: bool, is_in: bool) -> str:
    """Status-tinted scrim for left rail / bottom text (high opacity for contrast)."""
    if is_pending:
        return _STATUS_PENDING_SCRIM
    if is_in:
        return _STATUS_IN_SCRIM
    return _STATUS_NEUTRAL_SCRIM


def _status_bg(is_pending: bool, is_in: bool, *, grid: bool = True, even: bool = False) -> str | None:
    if is_pending and is_in:
        return _BG_PENDING_IN
    if is_pending:
        return _BG_PENDING
    if is_in:
        return _BG_IN
    if grid:
        return _BG_GRID
    return _ROW_ALT if even else None


def _prefetch(urls: list[str]) -> None:
    try:
        import requests
        for url in urls[:48]:
            if url:
                try:
                    r = requests.get(url, timeout=4, stream=True)
                    r.close()
                except Exception:
                    pass
    except Exception:
        pass


def row_is_downloadable(cell) -> bool:
    return str(cell or "").strip().lower() in {"yes", "y", "true", "1"}


def download_icon_style(downloadable: bool) -> tuple[str, str]:
    """Green = marked downloadable; yellow = try API anyway (deleted-account edge cases)."""
    if downloadable:
        return "#4ade80", "Download GLB (marked downloadable on Sketchfab)"
    return (
        "#facc15",
        "Try download — not marked downloadable (Download API may still work, e.g. Artist / deleted accounts)",
    )
