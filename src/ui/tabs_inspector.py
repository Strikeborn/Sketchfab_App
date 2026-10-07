"""3D Inspector tab — Viewer API scene/material/node export (not mesh GLB rip)."""
from __future__ import annotations

import flet as ft

from ui.viewer_dock import open_viewer_app
from ui.viewer_server import inspector_url, set_model, start_server
from ui.webview_support import webview_supported

_PANEL = "#0f172a"
_MUTED = "#94a3b8"


class InspectorTab(ft.Column):
    def __init__(self, page: ft.Page | None = None):
        super().__init__(expand=True, spacing=8)
        self._page = page
        self._uid = ""
        self._name = ""
        self._use_webview = webview_supported()

        self._uid_field = ft.TextField(
            label="Model UID",
            hint_text="32-char hex from Liked / Browse…",
            width=320,
            dense=True,
            text_size=13,
            on_submit=lambda e: self._load(),
        )
        self._name_field = ft.TextField(
            label="Title (optional)",
            width=280,
            dense=True,
            text_size=13,
        )
        self._status = ft.Text("", size=12, color=_MUTED)
        self._webview = (
            ft.WebView(url="about:blank", expand=True, enable_javascript=True)
            if self._use_webview
            else None
        )

        win_note = (
            "On Windows, inspector opens in Chrome/Edge (Flet has no in-app WebView here). "
            if not self._use_webview
            else ""
        )

        top = ft.Container(
            padding=10,
            bgcolor=_PANEL,
            border_radius=8,
            content=ft.Column(
                [
                    ft.Text("3D Inspector", size=15, weight=ft.FontWeight.BOLD),
                    ft.Text(
                        win_note
                        + "Pull scene hierarchy, node names, materials, and annotations via Sketchfab’s Viewer API. "
                        "Does not export mesh GLB — use yellow Try download for that. "
                        "Great for documenting deleted-account models still viewable online.",
                        size=11,
                        color=_MUTED,
                    ),
                    ft.Row(
                        [
                            self._uid_field,
                            self._name_field,
                            ft.ElevatedButton(
                                "Load",
                                icon=ft.Icons.PLAY_ARROW,
                                tooltip="Open inspector (Chrome/Edge on Windows)",
                                on_click=lambda e: self._load(),
                            ),
                            ft.OutlinedButton(
                                "Open window",
                                icon=ft.Icons.OPEN_IN_NEW,
                                tooltip="Chrome/Edge app with full inspector tools",
                                on_click=lambda e: self._open_window(),
                            ),
                        ],
                        spacing=8,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._status,
                ],
                spacing=8,
                tight=True,
            ),
        )

        stage = (
            ft.Container(expand=True, bgcolor="#000", border_radius=8, content=self._webview)
            if self._webview is not None
            else ft.Container(
                expand=True,
                bgcolor=_PANEL,
                border_radius=8,
                padding=24,
                content=ft.Column(
                    [
                        ft.Icon(ft.Icons.OPEN_IN_NEW, size=40, color=_MUTED),
                        ft.Text(
                            "Windows/Linux: click Load or Open window — inspector runs in Chrome/Edge, not inside Flet.\n\n"
                            "Export scene JSON for nodes/materials; screenshot for reference.",
                            size=13,
                            color=_MUTED,
                        ),
                    ],
                    spacing=12,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
            )
        )

        self.controls = [top, stage]

    def set_model(self, uid: str, name: str = "") -> None:
        self._uid = (uid or "").strip()
        self._name = (name or "").strip()
        self._uid_field.value = self._uid
        self._name_field.value = self._name
        try:
            self._uid_field.update()
            self._name_field.update()
        except Exception:
            pass

    def on_tab_shown(self) -> None:
        if self._uid and self._use_webview:
            self._load()

    def scroll_to_top(self) -> None:
        pass

    def _load(self) -> None:
        uid = (self._uid_field.value or "").strip()
        if not uid:
            self._status.value = "Enter a model UID."
            self.update()
            return
        name = (self._name_field.value or "").strip()
        self._uid = uid
        self._name = name
        set_model(uid, name)
        start_server()
        url = inspector_url(uid, name)
        if self._webview is not None:
            self._webview.url = url
            self._status.value = f"Inspector → {uid[:8]}…"
        else:
            ok = open_viewer_app(url, width=960, height=900)
            self._status.value = (
                f"Inspector window → {uid[:8]}…"
                if ok
                else "Could not launch browser — check Chrome/Edge."
            )
        try:
            self.update()
        except Exception:
            pass

    def _open_window(self) -> None:
        uid = (self._uid_field.value or self._uid or "").strip()
        if not uid:
            self._status.value = "Enter a model UID first."
            self.update()
            return
        name = (self._name_field.value or self._name or "").strip()
        set_model(uid, name)
        start_server()
        url = inspector_url(uid, name)
        ok = open_viewer_app(url, width=960, height=900)
        self._status.value = "Inspector window opened." if ok else "Could not launch browser — check Chrome/Edge."
        try:
            self.update()
        except Exception:
            pass
