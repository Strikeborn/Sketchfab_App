"""Middle-click hold-and-drag scrolling for Flet scrollables."""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

import flet as ft

_POLL_S = 0.008
_VK_MBUTTON = 0x04
_LOD_INTERVAL_S = 0.016  # ~60fps viewport/LOD while dragging


def _middle_down() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(_VK_MBUTTON) & 0x8000)
    except Exception:
        return False


def wrap_middle_drag_scroll(
    scrollable: ft.Control,
    *,
    expand: bool | int | None = True,
    on_scroll: Callable[[ft.OnScrollEvent], None] | None = None,
    on_offset: Callable[[float], None] | None = None,
) -> ft.Control:
    """
    Middle-mouse hold + drag scrolls vertically (grab cursor only while held).

    ``on_offset`` keeps LOD in sync when scroll_to skips OnScrollEvent.
    """
    state = {
        "offset": 0.0,
        "inside": False,
        "middle_dragging": False,
        "drag_start_y": None,
        "drag_start_off": 0.0,
        "watch_thread": None,
        "pending_off": None,
        "flush_scheduled": False,
        "gd": None,
        "hover_y": None,
        "last_lod_off": None,
        "last_lod_t": 0.0,
    }

    def _page():
        return getattr(scrollable, "page", None)

    def _notify_offset(off: float, *, force: bool = False) -> None:
        if not on_offset:
            return
        now = time.time()
        prev = state.get("last_lod_off")
        interval = 0.0 if force or state["middle_dragging"] else _LOD_INTERVAL_S
        if (
            not force
            and prev is not None
            and interval > 0
            and abs(off - float(prev)) < 1.0
            and (now - float(state.get("last_lod_t") or 0)) < interval
        ):
            return
        state["last_lod_off"] = off
        state["last_lod_t"] = now
        try:
            on_offset(float(off))
        except Exception:
            pass

    def _sync_scroll(e: ft.OnScrollEvent) -> None:
        if not state["middle_dragging"]:
            state["offset"] = float(e.pixels or 0)
        if on_scroll:
            on_scroll(e)

    if hasattr(scrollable, "on_scroll"):
        scrollable.on_scroll = _sync_scroll

    def _apply_scroll(offset: float) -> None:
        try:
            scrollable.scroll_to(offset=max(0.0, float(offset)), duration=0)
        except Exception:
            pass

    def _flush_pending() -> None:
        state["flush_scheduled"] = False
        off = state.get("pending_off")
        if off is None:
            return
        state["pending_off"] = None
        state["offset"] = off
        _apply_scroll(off)
        _notify_offset(off)

    def _queue_scroll(offset: float) -> None:
        state["pending_off"] = max(0.0, offset)
        if state["flush_scheduled"]:
            return
        state["flush_scheduled"] = True
        pg = _page()

        async def _do() -> None:
            _flush_pending()

        if pg:
            try:
                pg.run_task(_do)
            except Exception:
                _flush_pending()
        else:
            _flush_pending()

    def _set_cursor(grabbing: bool) -> None:
        gd = state.get("gd")
        if gd is None:
            return
        try:
            gd.mouse_cursor = ft.MouseCursor.GRABBING if grabbing else ft.MouseCursor.BASIC
            gd.update()
        except Exception:
            pass

    def _stop_middle_drag() -> None:
        if not state["middle_dragging"]:
            return
        state["middle_dragging"] = False
        state["drag_start_y"] = None
        _set_cursor(False)
        _notify_offset(float(state["offset"]), force=True)

    def _watch_loop() -> None:
        while state["inside"] or state["middle_dragging"]:
            mid = _middle_down()
            if mid and state["inside"]:
                if not state["middle_dragging"]:
                    state["middle_dragging"] = True
                    state["drag_start_y"] = state.get("hover_y")
                    state["drag_start_off"] = float(state["offset"])
                    _set_cursor(True)
                y = state.get("hover_y")
                origin_y = state.get("drag_start_y")
                if y is not None and origin_y is not None:
                    dy = float(y) - float(origin_y)
                    _queue_scroll(float(state["drag_start_off"]) - dy)
            elif state["middle_dragging"]:
                _stop_middle_drag()

            if not state["inside"] and not state["middle_dragging"]:
                break
            time.sleep(_POLL_S)

        state["watch_thread"] = None
        _stop_middle_drag()

    def _start_watch() -> None:
        if state["watch_thread"] is not None:
            return
        t = threading.Thread(target=_watch_loop, daemon=True, name="mid-drag-scroll")
        state["watch_thread"] = t
        t.start()

    def _on_enter(e: ft.HoverEvent) -> None:
        state["inside"] = True
        try:
            state["hover_y"] = float(getattr(e, "local_y", None) or 0)
        except (TypeError, ValueError):
            state["hover_y"] = None
        _start_watch()

    def _on_exit(_e: ft.HoverEvent) -> None:
        state["inside"] = False

    def _on_hover(e: ft.HoverEvent) -> None:
        try:
            state["hover_y"] = float(getattr(e, "local_y", None) or 0)
        except (TypeError, ValueError):
            return

    gd = ft.GestureDetector(
        content=scrollable,
        expand=expand,
        hover_interval=4,
        mouse_cursor=ft.MouseCursor.BASIC,
        on_enter=_on_enter,
        on_hover=_on_hover,
        on_exit=_on_exit,
    )
    state["gd"] = gd
    gd.is_middle_dragging = lambda: bool(state["middle_dragging"])  # type: ignore[attr-defined]
    return gd
