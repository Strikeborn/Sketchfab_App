"""Safe Flet control updates — skip unmounted controls (avoids AssertionError spam)."""
from __future__ import annotations

import flet as ft


def mounted(control: ft.Control | None) -> bool:
    return bool(control is not None and getattr(control, "page", None))


def safe_page_update(page: ft.Page | None) -> bool:
    """Full-page update without crashing when a child control lost its Flet uid."""
    if page is None:
        return False
    try:
        page.update()
        return True
    except (AssertionError, RuntimeError, Exception):
        return False


def safe_update(*controls: ft.Control | None, fallback: ft.Control | None = None) -> bool:
    ok = False
    for ctrl in controls:
        if not ctrl:
            continue
        try:
            if mounted(ctrl):
                ctrl.update()
                ok = True
        except (AssertionError, RuntimeError, Exception):
            continue
    if not ok and fallback is not None:
        try:
            if mounted(fallback):
                fallback.update()
                return True
        except (AssertionError, RuntimeError, Exception):
            pass
    return ok
