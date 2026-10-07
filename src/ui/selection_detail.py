"""Bottom-right panel: details for the currently selected card(s)."""
from __future__ import annotations

import re
import threading

import flet as ft

_BG = "#0f172a"
_MUTED = "#94a3b8"
_TEXT = "#e2e8f0"
_LINK = "#38bdf8"

_URL_RE = re.compile(r"(https?://[^\s<>\")\]]+)", re.IGNORECASE)


def _strip_trailing_punct(url: str) -> str:
    return url.rstrip(".,);]}")


def _description_block(text: str, *, size: int = 11) -> ft.Control:
    text = (text or "").strip()
    if not text:
        return ft.Text("(no description)", size=size, color=_MUTED)

    parts = _URL_RE.split(text)
    if len(parts) == 1:
        return ft.Text(text, size=size, color=_TEXT, selectable=True)

    chunks: list[ft.Control] = []
    buf = ""
    for i, part in enumerate(parts):
        if not part:
            continue
        if i % 2 == 1:
            if buf:
                chunks.append(ft.Text(buf, size=size, color=_TEXT, selectable=True))
                buf = ""
            url = _strip_trailing_punct(part)
            label = url if len(url) <= 72 else url[:69] + "…"
            chunks.append(
                ft.TextButton(
                    text=label,
                    url=url,
                    style=ft.ButtonStyle(color=_LINK, padding=ft.padding.symmetric(horizontal=0, vertical=0)),
                    tooltip=url,
                )
            )
        else:
            buf += part
    if buf:
        chunks.append(ft.Text(buf, size=size, color=_TEXT, selectable=True))
    return ft.Column(chunks, spacing=2, tight=True)


class SelectionDetailPane(ft.Column):
    def __init__(self, *, on_fetch_detail=None):
        """
        on_fetch_detail(uid) -> dict with optional keys:
          name, description, author, license, faceCount, tags, categories, url
        """
        self._on_fetch = on_fetch_detail
        self._fetch_gen = 0
        self._title = ft.Text("Selection", weight=ft.FontWeight.BOLD, size=12)
        self._body = ft.Column(
            [
                ft.Text(
                    "Select a card to see details.",
                    size=11,
                    color=_MUTED,
                )
            ],
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        super().__init__(
            expand=True,
            spacing=4,
            controls=[
                self._title,
                ft.Container(
                    expand=True,
                    bgcolor=_BG,
                    border_radius=6,
                    padding=8,
                    border=ft.border.all(1, "#334155"),
                    alignment=ft.alignment.top_left,
                    content=self._body,
                ),
            ],
        )

    def set_fetch_callback(self, cb) -> None:
        self._on_fetch = cb

    def show_selection(self, uids: list[str], *, row: dict | None = None) -> None:
        uids = [u for u in (uids or []) if u]
        self._fetch_gen += 1
        gen = self._fetch_gen

        if not uids:
            self._title.value = "Selection"
            self._body.controls = [
                ft.Text("Select a card to see details.", size=11, color=_MUTED),
            ]
            self._safe_update()
            return

        if len(uids) > 1:
            self._title.value = "Selection"
            self._body.controls = [
                ft.Text("Multiple cards selected", size=13, weight=ft.FontWeight.W_600, color=_TEXT),
                ft.Text(f"{len(uids)} models selected.", size=11, color=_MUTED),
                ft.Text("Details for multi-select coming later.", size=11, color=_MUTED),
            ]
            self._safe_update()
            return

        uid = uids[0]
        name = (row or {}).get("Name") or (row or {}).get("name") or uid
        self._title.value = "Selection"
        self._body.controls = self._skeleton(name, uid, row)
        self._safe_update()

        if not self._on_fetch:
            return

        def _work():
            detail = {}
            try:
                detail = self._on_fetch(uid) or {}
            except Exception as e:
                detail = {"error": str(e)}
            if gen != self._fetch_gen:
                return
            self._body.controls = self._detail_controls(uid, name, row, detail)
            self._safe_update()

        threading.Thread(target=_work, daemon=True).start()

    def _skeleton(self, name: str, uid: str, row: dict | None) -> list[ft.Control]:
        controls = [
            ft.Text(str(name), size=13, weight=ft.FontWeight.W_700, color=_TEXT),
            ft.Text(uid, size=10, color=_MUTED, selectable=True),
        ]
        if row:
            controls.extend(self._local_fields(row))
        controls.append(ft.Text("Loading description…", size=11, color=_MUTED))
        return controls

    def _local_fields(self, row: dict) -> list[ft.Control]:
        bits = []
        for label, key in (
            ("Author", "Author"),
            ("License", "License"),
            ("Faces", "Face Count"),
            ("Categories", "Categories"),
            ("Tags", "Tags"),
            ("Assigned", "Assigned Collection(s)"),
        ):
            val = row.get(key)
            if val is None or (isinstance(val, float) and str(val) == "nan"):
                continue
            s = str(val).strip()
            if not s or s.lower() == "nan":
                continue
            bits.append(ft.Text(f"{label}: {s}", size=11, color=_TEXT))
        return bits

    def _detail_controls(self, uid: str, name: str, row: dict | None, detail: dict) -> list[ft.Control]:
        if detail.get("error") and not detail.get("description"):
            base = self._skeleton(name, uid, row)[:-1]
            base.append(ft.Text(f"Could not load: {detail['error']}", size=11, color="#f87171"))
            return base

        d_name = detail.get("name") or name
        desc = (detail.get("description") or "").strip()
        controls: list[ft.Control] = [
            ft.Text(str(d_name), size=13, weight=ft.FontWeight.W_700, color=_TEXT),
            ft.Text(uid, size=10, color=_MUTED, selectable=True),
        ]
        if row:
            controls.extend(self._local_fields(row))
        for label, key in (
            ("Author", "author"),
            ("License", "license"),
            ("Faces", "faceCount"),
            ("Views", "viewCount"),
            ("Likes", "likeCount"),
            ("Downloads", "downloadCount"),
        ):
            if detail.get(key) in (None, ""):
                continue
            if row and key in ("author", "license", "faceCount"):
                continue
            val = detail[key]
            if isinstance(val, (int, float)) and key.endswith("Count"):
                controls.append(ft.Text(f"{label}: {int(val):,}", size=11, color=_TEXT))
            else:
                controls.append(ft.Text(f"{label}: {val}", size=11, color=_TEXT))

        controls.append(ft.Divider(height=1, color="#334155"))
        controls.append(ft.Text("Description", size=11, weight=ft.FontWeight.W_600, color=_MUTED))
        controls.append(_description_block(desc))
        url = detail.get("url") or f"https://sketchfab.com/3d-models/{uid}"
        controls.append(
            ft.TextButton("Open on Sketchfab", url=url, style=ft.ButtonStyle(padding=0, color=_LINK))
        )
        return controls

    def _safe_update(self) -> None:
        try:
            if self.page:
                self.update()
        except Exception:
            pass
