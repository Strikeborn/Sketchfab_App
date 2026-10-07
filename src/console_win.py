"""Windows console show/hide (for tray minimize)."""
from __future__ import annotations

import sys


def set_console_visible(visible: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            # SW_HIDE=0, SW_SHOW=5
            ctypes.windll.user32.ShowWindow(hwnd, 5 if visible else 0)
    except Exception:
        pass
