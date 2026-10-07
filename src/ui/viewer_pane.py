"""Right-side Sketchfab viewer dock — tools + Chromium app (Windows) / WebView (macOS)."""
from __future__ import annotations

import flet as ft

from sketchfab_client import SketchfabClient
from ui.viewer_dock import close_viewer_app, open_viewer_app
from ui.viewer_server import set_model, start_server, viewer_url as build_viewer_url
from ui.webview_support import webview_supported


class ViewerPane(ft.Container):
    """
    Seamless workflow:
      • Card 3D overlay → opens this dock + Sketchfab Viewer API window
      • Pane shows model meta + quick tools; live orbit/materials live in the docked viewer
    """

    def __init__(self, page: ft.Page | None = None):
        self._page = page
        self._uid = ""
        self._name = ""
        self._thumb = ""
        self._viewer_url = ""
        self._external_url = ""
        self._use_webview = webview_supported()
        self._webview = None

        self._title = ft.Text("3D Preview", size=13, weight=ft.FontWeight.BOLD, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
        self._subtitle = ft.Text("Press 3D on a card", size=10, color=ft.Colors.GREY_500)
        self._status = ft.Text("", size=10, color=ft.Colors.GREY_500)
        self._thumb_img = ft.Image(
            src="",
            fit=ft.ImageFit.COVER,
            height=140,
            border_radius=6,
            visible=False,
            error_content=ft.Icon(ft.Icons.VIEW_IN_AR, size=40, color=ft.Colors.GREY_600),
        )

        self._open_dock_btn = ft.ElevatedButton(
            "Open 3D viewer",
            icon=ft.Icons.VIEW_IN_AR,
            on_click=lambda e: self._open_dock(force=True),
        )
        self._site_btn = ft.OutlinedButton(
            "Full Sketchfab page",
            icon=ft.Icons.OPEN_IN_NEW,
            on_click=lambda e: self._open_site(),
        )
        self._close_btn = ft.IconButton(
            icon=ft.Icons.CLOSE,
            icon_size=18,
            tooltip="Close preview dock",
            on_click=lambda e: self.hide(),
        )

        if self._use_webview:
            self._webview = ft.WebView(
                url="about:blank",
                expand=True,
                enable_javascript=True,
                bgcolor="#0b1220",
            )
            stage = ft.Container(expand=True, content=self._webview, bgcolor="#0b1220", border_radius=6)
        else:
            stage = ft.Container(
                expand=True,
                bgcolor="#0b1220",
                border_radius=6,
                padding=10,
                content=ft.Column(
                    [
                        self._thumb_img,
                        ft.Text(
                            "Live orbit + materials open in a docked Chrome/Edge window "
                            "(Sketchfab Viewer API — same player as the site).",
                            size=11,
                            color=ft.Colors.GREY_400,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        self._open_dock_btn,
                        ft.Text(
                            "Tools in that window: wireframe, inspector, annotations, "
                            "background, materials list, screenshot.",
                            size=10,
                            color=ft.Colors.GREY_600,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    ],
                    spacing=10,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
            )

        tools = ft.Row(
            [
                ft.TextButton("Wireframe", icon=ft.Icons.GRID_ON, on_click=lambda e: self._open_dock(force=True)),
                ft.TextButton("Materials", icon=ft.Icons.PALETTE, on_click=lambda e: self._open_dock(force=True)),
                ft.TextButton("Inspect", icon=ft.Icons.TRAVEL_EXPLORE, on_click=lambda e: self._open_dock(force=True)),
            ],
            wrap=True,
            spacing=0,
        )

        super().__init__(
            width=0,
            visible=False,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            border=ft.border.only(left=ft.BorderSide(1, "#334155")),
            padding=ft.padding.only(left=8, right=4, top=4, bottom=4),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column([self._title, self._subtitle, self._status], spacing=1, expand=True),
                            self._close_btn,
                        ],
                        spacing=0,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    tools,
                    stage,
                    ft.Row([self._site_btn], alignment=ft.MainAxisAlignment.START),
                ],
                expand=True,
                spacing=6,
            ),
        )

    def set_page(self, page: ft.Page) -> None:
        self._page = page

    def show_model(self, uid: str, name: str = "", viewer_url: str = "", thumb_url: str = "") -> None:
        if not uid:
            return
        self._uid = uid.strip()
        self._name = (name or uid)[:80]
        self._thumb = thumb_url or ""
        embed = viewer_url if viewer_url and "/embed" in viewer_url else SketchfabClient.embed_url(self._uid)
        self._viewer_url = embed
        self._external_url = f"https://sketchfab.com/3d-models/{self._uid}"

        self._title.value = self._name
        self._subtitle.value = self._uid
        self._status.value = "Opening Sketchfab viewer…"
        if self._thumb:
            self._thumb_img.src = self._thumb
            self._thumb_img.visible = True

        self.visible = True
        self.width = 360
        try:
            self.update()
        except Exception:
            pass

        set_model(self._uid, self._name, self._thumb)
        start_server()
        url = build_viewer_url(self._uid, self._name, self._thumb)

        if self._use_webview and self._webview is not None:
            self._webview.url = url
            self._status.value = "In-pane WebView (Viewer API)"
        else:
            ok = self._open_dock(force=True, url=url)
            self._status.value = "Docked Chrome/Edge viewer" if ok else "Could not launch browser — click Open 3D viewer"

        try:
            self.update()
        except Exception:
            if self._page:
                self._page.update()

    def _open_dock(self, *, force: bool = False, url: str | None = None) -> bool:
        if not self._uid and not url:
            return False
        set_model(self._uid, self._name, self._thumb)
        target = url or build_viewer_url(self._uid, self._name, self._thumb)
        left = top = None
        pg = self._page or self.page
        if pg is not None:
            try:
                # Sit the dock just right of the main window when we know its placement.
                w = int(getattr(pg.window, "width", None) or 1200)
                left = int(getattr(pg.window, "left", None) or 40) + max(600, w - 80)
                top = int(getattr(pg.window, "top", None) or 40)
            except Exception:
                left, top = None, None
        return open_viewer_app(target, width=520, height=920, left=left, top=top)

    def _open_site(self) -> None:
        url = self._external_url or self._viewer_url
        pg = self._page or self.page
        if url and pg:
            # Full site page — denser UI / download originals when Sketchfab allows.
            pg.launch_url(url)

    def hide(self) -> None:
        self.visible = False
        self.width = 0
        close_viewer_app()
        if self._use_webview and self._webview is not None:
            self._webview.url = "about:blank"
        self._thumb_img.src = ""
        self._thumb_img.visible = False
        self._uid = ""
        try:
            self.update()
        except Exception:
            if self._page:
                self._page.update()
