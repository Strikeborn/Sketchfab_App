"""Native-style custom title bar + system tray."""
from __future__ import annotations

import logging
import queue
import sys
import threading
from typing import Callable, Literal

import flet as ft

from window_win import (
    hide_app_to_tray,
    hide_was_maximized,
    minimize_app,
    restore_app_from_tray,
    suppress_relayout,
    toggle_maximize_app,
)

logger = logging.getLogger(__name__)

_tray_icon = None
_tray_thread = None
_tray_lock = threading.Lock()

_TITLE_H = 32
_CAPTION_W = 46
# Win11-ish dark chrome
_TITLE_BG = "#202020"
_TRAY_BG = "#3f3f3f"
_EXIT_BG = "#c42b1c"
_EXIT_HOVER = "#e81123"

CaptionMode = Literal["native", "hybrid", "frameless"]


def configure_window(page: ft.Page, *, mode: CaptionMode) -> None:
    """native = stock Windows; hybrid = stock bar + hacked × buttons; frameless = full custom strip."""
    page.spacing = 0
    try:
        if mode == "frameless":
            page.padding = 0
            page.window.title_bar_hidden = True
            page.window.title_bar_buttons_hidden = True
        elif mode == "hybrid":
            page.padding = ft.padding.only(left=10, right=10, bottom=10, top=0)
            page.window.title_bar_hidden = False
            page.window.title_bar_buttons_hidden = True
        else:
            page.padding = 10
            page.window.title_bar_hidden = False
            page.window.title_bar_buttons_hidden = False
    except Exception:
        logger.exception("configure_window failed")


def pick_caption_mode(*, dual_close: bool) -> CaptionMode:
    """Frameless strip is the only reliable way to show two × buttons in Flet on Windows."""
    if dual_close:
        return "frameless"
    return "native"


def _cap(
    icon: str,
    tooltip: str,
    on_click,
    *,
    icon_size: int = 16,
    icon_color=ft.Colors.WHITE70,
    bgcolor: str = _TITLE_BG,
    hover_bg: str | None = None,
) -> ft.Container:
    def _click(e):
        on_click(e)

    return ft.Container(
        width=_CAPTION_W,
        height=_TITLE_H,
        bgcolor=bgcolor,
        alignment=ft.alignment.center,
        tooltip=tooltip,
        on_click=_click,
        content=ft.Icon(icon, size=icon_size, color=icon_color),
    )


def _caption_row(
    page: ft.Page,
    *,
    title: str,
    on_hide_tray: Callable[..., None],
    on_exit: Callable[..., None],
    schedule_relayout: Callable[[float], None] | None = None,
) -> ft.Row:
    def _min(_e):
        suppress_relayout(1.0)
        minimize_app(title)

    def _max(_e):
        suppress_relayout(2.0)
        toggle_maximize_app(title)
        if schedule_relayout:
            schedule_relayout(1.85)

    def _exit(_e):
        on_exit(_e)

    return ft.Row(
        [
            _cap(ft.Icons.REMOVE, "Minimize", _min, icon_size=14),
            _cap(ft.Icons.CROP_SQUARE, "Maximize / restore", _max, icon_size=10),
            _cap(
                ft.Icons.CLOSE,
                "Hide to tray (keeps syncing)",
                on_hide_tray,
                icon_color=ft.Colors.WHITE70,
                bgcolor="#4a4a4a",
                hover_bg="#666666",
            ),
            _cap(
                ft.Icons.CLOSE,
                "Exit — stop the app",
                _exit,
                icon_color=ft.Colors.WHITE,
                bgcolor=_EXIT_BG,
                hover_bg=_EXIT_HOVER,
            ),
        ],
        spacing=0,
        tight=True,
    )


def build_hybrid_caption_overlay(
    page: ft.Page,
    *,
    title: str,
    on_hide_tray: Callable[..., None],
    on_exit: Callable[..., None],
    schedule_relayout: Callable[[float], None] | None = None,
) -> ft.Container:
    """Hide native ×/—/□; draw our gray+red × in the Windows title-bar band (Win32 ops — no page.update)."""
    return ft.Container(
        height=_TITLE_H,
        margin=ft.margin.only(top=-_TITLE_H),
        alignment=ft.alignment.center_right,
        content=_caption_row(
            page,
            title=title,
            on_hide_tray=on_hide_tray,
            on_exit=on_exit,
            schedule_relayout=schedule_relayout,
        ),
    )


def build_frameless_title_bar(
    page: ft.Page,
    *,
    title: str,
    on_hide_tray: Callable[..., None],
    on_exit: Callable[..., None],
    schedule_relayout: Callable[[float], None] | None = None,
) -> ft.Container:
    title_label = ft.Container(
        expand=True,
        height=_TITLE_H,
        padding=ft.padding.only(left=12),
        alignment=ft.alignment.center_left,
        content=ft.Text(
            title,
            size=12,
            color=ft.Colors.GREY_300,
            weight=ft.FontWeight.W_500,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
        ),
    )

    return ft.Container(
        height=_TITLE_H,
        bgcolor=_TITLE_BG,
        padding=0,
        content=ft.Row(
            [
                ft.WindowDragArea(
                    expand=True,
                    maximizable=True,
                    content=title_label,
                ),
                _caption_row(
                    page,
                    title=title,
                    on_hide_tray=on_hide_tray,
                    on_exit=on_exit,
                    schedule_relayout=schedule_relayout,
                ),
            ],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


# Back-compat alias
build_custom_title_bar = build_frameless_title_bar


def hide_to_tray(page: ft.Page, *, title_hint: str = "") -> bool:
    hint = title_hint or getattr(page, "title", "") or "Sketchfab"
    if sys.platform == "win32" and hide_app_to_tray(hint):
        return True
    try:
        page.window.skip_task_bar = True
        page.window.minimized = True
        return True
    except Exception:
        logger.exception("hide_to_tray failed")
        return False


def restore_from_tray(page: ft.Page, *, title_hint: str = "") -> None:
    hint = title_hint or getattr(page, "title", "") or "Sketchfab"
    if sys.platform == "win32" and restore_app_from_tray(hint):
        try:
            page.window.skip_task_bar = False
            page.window.minimized = False
            page.window.maximized = hide_was_maximized()
            page.window.to_front()
        except Exception:
            pass
        return
    try:
        page.window.skip_task_bar = False
        page.window.minimized = False
        page.window.to_front()
    except Exception:
        logger.exception("restore_from_tray failed")


def _stop_tray_icon_unlocked() -> None:
    global _tray_icon, _tray_thread
    icon = _tray_icon
    _tray_icon = None
    _tray_thread = None
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass


def stop_tray_icon() -> None:
    with _tray_lock:
        _stop_tray_icon_unlocked()


def start_tray_icon(
    *,
    title: str,
    ui_queue: queue.Queue[str],
) -> bool:
    global _tray_icon, _tray_thread
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("pystray/Pillow not installed — tray icon unavailable")
        return False

    with _tray_lock:
        if _tray_thread and _tray_thread.is_alive() and _tray_icon is not None:
            return True
        _stop_tray_icon_unlocked()

    def _make_image():
        img = Image.new("RGB", (64, 64), color=(15, 23, 42))
        draw = ImageDraw.Draw(img)
        draw.ellipse((12, 12, 52, 52), fill=(56, 189, 248))
        return img

    def _run():
        global _tray_icon

        def show(_icon=None, _item=None):
            try:
                ui_queue.put_nowait("show")
            except Exception:
                pass

        def quit_app(_icon=None, _item=None):
            try:
                ui_queue.put_nowait("exit")
            except Exception:
                pass

        menu = pystray.Menu(
            pystray.MenuItem("Show", show, default=True),
            pystray.MenuItem("Exit", quit_app),
        )
        icon = pystray.Icon(title, _make_image(), title, menu)
        with _tray_lock:
            _tray_icon = icon
        icon.run()

    t = threading.Thread(target=_run, daemon=True, name="pystray")
    with _tray_lock:
        _tray_thread = t
    t.start()
    return True


def make_tray_ui_queue() -> queue.Queue[str]:
    return queue.Queue()
