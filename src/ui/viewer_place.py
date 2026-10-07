"""Place the docked Chrome/Edge viewer over a grid card's exposed image."""
from __future__ import annotations

import sys
from typing import Any

# Defaults match Liked/Browse card chrome (left rail + bottom meta scrim).
DEFAULT_RAIL_W = 40
DEFAULT_META_H = 68


def dpi_scale() -> float:
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes

        return max(1.0, float(ctypes.windll.user32.GetDpiForSystem()) / 96.0)
    except Exception:
        return 1.0


def _cursor_pos() -> tuple[int, int] | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    except Exception:
        return None


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def image_overlay_size(
    card_w: int | float,
    card_h: int | float,
    *,
    rail_w: int = DEFAULT_RAIL_W,
    meta_h: int = DEFAULT_META_H,
    titlebar: int = 32,
) -> tuple[int, int, int, int]:
    """
    Logical px: (inset_x, inset_y, content_w, outer_h_with_titlebar)
    Exposed thumb = card minus left rail buttons and bottom text block.
    """
    cw = max(96.0, float(card_w))
    ch = max(72.0, float(card_h))
    rw = max(0, int(rail_w))
    mh = max(0, min(int(meta_h), int(ch * 0.45)))
    content_w = max(96.0, cw - rw)
    content_h = max(80.0, ch - mh)
    return rw, 0, int(round(content_w)), int(round(content_h + titlebar))


def screen_rect_for_card_image(
    page: Any,
    e: Any,
    card_w: int | float,
    card_h: int | float,
    *,
    rail_w: int = DEFAULT_RAIL_W,
    meta_h: int = DEFAULT_META_H,
    titlebar: int = 32,
) -> dict[str, int] | None:
    """
    Screen rect for Chrome --app over the *exposed image* (not rail / not bottom text).
    Card origin from HoverEvent; size from column-driven card_w/card_h.
    """
    inset_x, inset_y, content_w, outer_h = image_overlay_size(
        card_w, card_h, rail_w=rail_w, meta_h=meta_h, titlebar=titlebar
    )
    scale = dpi_scale()
    width = max(120, int(round(content_w * scale)))
    height = max(100, int(round(outer_h * scale)))

    gx = _f(getattr(e, "global_x", None))
    gy = _f(getattr(e, "global_y", None))
    lx = _f(getattr(e, "local_x", None))
    ly = _f(getattr(e, "local_y", None))

    card_x = card_y = None
    if gx is not None and gy is not None and page is not None:
        try:
            win_l = float(getattr(page.window, "left", None) or 0)
            win_t = float(getattr(page.window, "top", None) or 0)
        except Exception:
            win_l, win_t = 0.0, 0.0
        card_x = win_l + gx - (lx or 0.0)
        card_y = win_t + gy - (ly or 0.0)
    else:
        cur = _cursor_pos()
        if cur is None:
            return None
        cx, cy = cur
        # Cursor is already physical; convert card insets in logical→physical.
        if lx is not None and ly is not None:
            left = int(round(cx - lx * scale + inset_x * scale))
            top = int(round(cy - ly * scale + inset_y * scale))
            return {"left": left, "top": top, "width": width, "height": height}
        # Unknown offset inside card — pin to image zone around cursor.
        left = int(cx - width // 2)
        top = int(cy - height // 2)
        return {"left": left, "top": top, "width": width, "height": height}

    left = int(round((card_x + inset_x) * scale))
    top = int(round((card_y + inset_y) * scale))
    return {"left": left, "top": top, "width": width, "height": height}


# Back-compat names used by older call sites
def screen_rect_from_hover(page, e, card_w, card_h, **kwargs):
    return screen_rect_for_card_image(page, e, card_w, card_h, **kwargs)


def screen_rect_from_cursor(card_w, card_h, **kwargs):
    class _E:
        global_x = None
        global_y = None
        local_x = kwargs.get("local_x")
        local_y = kwargs.get("local_y")

    return screen_rect_for_card_image(None, _E(), card_w, card_h)
