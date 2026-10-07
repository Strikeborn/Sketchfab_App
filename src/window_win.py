"""Win32 window control — instant ops without Flet page.update() (slow with huge grids)."""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

_cached_hwnd: int | None = None
_cached_title_hint: str = ""
_hide_was_maximized = False
_WIN32_TYPED = False
_relayout_suppress_until: float = 0.0

SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9

WM_CLOSE = 0x0010
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2


def hide_was_maximized() -> bool:
    return _hide_was_maximized


def _ensure_win32_types() -> None:
    """HWND is 64-bit on Win64 — default ctypes int conversion overflows."""
    global _WIN32_TYPED
    if _WIN32_TYPED or sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.ReleaseCapture.argtypes = []
    user32.ReleaseCapture.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    _WIN32_TYPED = True


def _user32():
    _ensure_win32_types()
    return ctypes.windll.user32


def _hwnd_int(hwnd) -> int:
    return int(hwnd) if hwnd else 0


def _window_title(hwnd) -> str:
    user32 = _user32()
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _find_all_windows(title_hint: str = "Sketchfab") -> list[int]:
    if sys.platform != "win32":
        return []
    hint = (title_hint or "Sketchfab").casefold()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        try:
            title = _window_title(hwnd)
            if title and hint in title.casefold():
                found.append(_hwnd_int(hwnd))
        except Exception:
            pass
        return True

    _user32().EnumWindows(_enum, 0)
    return found


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    kernel32 = ctypes.windll.kernel32
    PROCESS_TERMINATE = 0x0001
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not h:
        return
    try:
        kernel32.TerminateProcess(h, 0)
    finally:
        kernel32.CloseHandle(h)


def _child_pids(root_pid: int) -> set[int]:
    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    pids = {int(root_pid)}

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    for _ in range(4):
        grew = False
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == -1:
            break
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if kernel32.Process32First(snap, ctypes.byref(pe)):
            while True:
                if int(pe.th32ParentProcessID) in pids:
                    pid = int(pe.th32ProcessID)
                    if pid not in pids:
                        pids.add(pid)
                        grew = True
                if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(snap)
        if not grew:
            break
    return pids


def _exe_basename(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(h)
    return ""


def kill_stuck_flet_windows(
    title_hint: str = "Sketchfab",
    *,
    protect_root_pid: int | None = None,
) -> int:
    """Close orphaned Flet desktop shells (title-bar ghost after ExitProcess)."""
    if sys.platform != "win32":
        return 0
    global _cached_hwnd, _cached_title_hint
    _cached_hwnd = None
    _cached_title_hint = ""
    protected: set[int] = set()
    if protect_root_pid is not None:
        protected = _child_pids(int(protect_root_pid))
    user32 = _user32()
    killed = 0
    pids: set[int] = set()
    for hwnd in _find_all_windows(title_hint):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            pids.add(int(pid.value))
    time.sleep(0.15)
    for hwnd in _find_all_windows(title_hint):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            pids.add(int(pid.value))
    for pid in pids:
        if pid in protected:
            continue
        _terminate_pid(pid)
        killed += 1
    return killed


def suppress_relayout(seconds: float = 1.5) -> None:
    """Skip heavy grid relayout while caption drag/maximize runs."""
    global _relayout_suppress_until
    _relayout_suppress_until = max(_relayout_suppress_until, time.perf_counter() + max(0.0, seconds))


def relayout_suppressed() -> bool:
    return time.perf_counter() < _relayout_suppress_until


def _fast_kill_flet(title_hint: str = "Sketchfab") -> None:
    global _cached_hwnd, _cached_title_hint
    _cached_hwnd = None
    _cached_title_hint = ""
    for pid in _child_pids(os.getpid()):
        if pid != os.getpid() and "flet" in _exe_basename(pid):
            _terminate_pid(pid)
    if sys.platform != "win32":
        return
    user32 = _user32()
    for hwnd in _find_all_windows(title_hint):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value and int(pid.value) != os.getpid():
            _terminate_pid(int(pid.value))


def shutdown_flet_desktop(title_hint: str = "Sketchfab") -> None:
    """Close Flet View (flet.exe) before killing Python — prevents title-bar ghosts."""
    if sys.platform != "win32":
        return
    global _cached_hwnd, _cached_title_hint
    _cached_hwnd = None
    _cached_title_hint = ""
    user32 = _user32()
    for hwnd in _find_all_windows(title_hint):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    for pid in _child_pids(os.getpid()):
        if pid == os.getpid():
            continue
        if "flet" in _exe_basename(pid):
            _terminate_pid(pid)
    time.sleep(0.08)
    for hwnd in _find_all_windows(title_hint):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            _terminate_pid(int(pid.value))


def find_app_window(title_hint: str = "Sketchfab", *, refresh: bool = False) -> int | None:
    global _cached_hwnd, _cached_title_hint
    if sys.platform != "win32":
        return None

    user32 = _user32()
    if (
        not refresh
        and _cached_hwnd
        and _cached_title_hint == title_hint
        and user32.IsWindow(_cached_hwnd)
    ):
        return _cached_hwnd

    found = _find_all_windows(title_hint)
    if not found:
        return None

    rect = wintypes.RECT()
    best = found[0]
    best_area = -1
    for hwnd in found:
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area > best_area:
                best_area = area
                best = hwnd

    _cached_hwnd = best
    _cached_title_hint = title_hint
    return best


def minimize_app(title_hint: str = "Sketchfab") -> bool:
    if sys.platform != "win32":
        return False
    hwnd = find_app_window(title_hint)
    if not hwnd:
        return False
    return bool(_user32().ShowWindow(hwnd, SW_MINIMIZE))


def toggle_maximize_app(title_hint: str = "Sketchfab") -> bool:
    if sys.platform != "win32":
        return False
    hwnd = find_app_window(title_hint)
    if not hwnd:
        return False
    user32 = _user32()
    if user32.IsZoomed(hwnd):
        return bool(user32.ShowWindow(hwnd, SW_RESTORE))
    return bool(user32.ShowWindow(hwnd, SW_MAXIMIZE))


def hide_app_to_tray(title_hint: str = "Sketchfab") -> bool:
    """Hide window completely — remember maximize so restore doesn't span monitors."""
    global _hide_was_maximized
    if sys.platform != "win32":
        return False
    hwnd = find_app_window(title_hint)
    if not hwnd:
        return False
    user32 = _user32()
    _hide_was_maximized = bool(user32.IsZoomed(hwnd))
    return bool(user32.ShowWindow(hwnd, SW_HIDE))


def restore_app_from_tray(title_hint: str = "Sketchfab") -> bool:
    global _hide_was_maximized
    if sys.platform != "win32":
        return False
    hwnd = find_app_window(title_hint, refresh=True)
    if not hwnd:
        return False
    user32 = _user32()
    user32.ShowWindow(hwnd, SW_SHOW)
    if _hide_was_maximized:
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
    user32.SetForegroundWindow(hwnd)
    return True


def start_window_drag(title_hint: str = "Sketchfab") -> bool:
    """Native caption drag — avoids Flet start_dragging (laggy with huge grids)."""
    if sys.platform != "win32":
        return False
    hwnd = find_app_window(title_hint)
    if not hwnd:
        return False
    user32 = _user32()
    user32.ReleaseCapture()
    user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
    return True


def force_exit(title_hint: str = "Sketchfab", *, fast: bool = True) -> None:
    """Exit Python + Flet View together (avoids orphaned flet.exe title strip)."""
    try:
        if fast:
            _fast_kill_flet(title_hint)
        else:
            shutdown_flet_desktop(title_hint)
    except Exception:
        logger.exception("force_exit cleanup failed")
    if sys.platform == "win32":
        ctypes.windll.kernel32.ExitProcess(0)
    os._exit(0)


def close_app_window(title_hint: str = "Sketchfab") -> bool:
    if sys.platform != "win32":
        return False
    hwnd = find_app_window(title_hint)
    if not hwnd:
        return False
    return bool(_user32().PostMessageW(hwnd, WM_CLOSE, 0, 0))
