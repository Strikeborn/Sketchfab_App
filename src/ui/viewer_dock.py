"""Open / reuse a Chromium app window for the Sketchfab viewer dock."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_proc: subprocess.Popen | None = None
_last_url = ""
_PROFILE = Path(tempfile.gettempdir()) / "sketchfab_collections_viewer_chrome"
_BROWSER_EXES = {"chrome.exe", "msedge.exe", "chromium.exe", "chrome", "msedge", "chromium"}
_WIN32_TYPED = False


def _ensure_win32_types() -> None:
    """HWND is 64-bit on Win64 — default ctypes int conversion overflows."""
    global _WIN32_TYPED
    if _WIN32_TYPED or sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    _WIN32_TYPED = True


def _chrome_candidates() -> list[str]:
    found: list[str] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        for p in (
            Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ):
            if p.is_file():
                found.append(str(p))
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "msedge"):
            hit = shutil.which(name)
            if hit:
                found.append(hit)
    return found


def _child_pids_only(root_pid: int) -> set[int]:
    """Descend only — never walk up (walking up swallowed the Flet/Python HWND)."""
    import ctypes
    from ctypes import wintypes

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


def _exe_name(pid: int) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name.lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _force_window_rect(pid: int, left: int, top: int, width: int, height: int) -> bool:
    """
    Pin only the Chrome/Edge --app HWND. Never touch the Flet/Python window.
    Also set TOPMOST so the overlay stays above the desktop app.
    """
    if sys.platform != "win32" or pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        _ensure_win32_types()
        user32 = ctypes.windll.user32
        our_pid = os.getpid()
        pids = _child_pids_only(pid)
        pids.discard(our_pid)
        # Keep only browser executables in the tree.
        browser_pids = {p for p in pids if _exe_name(p) in _BROWSER_EXES}
        if not browser_pids:
            browser_pids = pids

        candidates: list[tuple[int, int, str]] = []  # hwnd, area, title

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lp):  # noqa: N802
            try:
                proc = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc))
                wpid = int(proc.value)
                if wpid == our_pid or wpid not in browser_pids:
                    return True
                if _exe_name(wpid) not in _BROWSER_EXES:
                    return True
                if not user32.IsWindowVisible(hwnd):
                    return True
                if user32.GetWindow(hwnd, 4):  # GW_OWNER
                    return True
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                if "Chrome_WidgetWin" not in (cls.value or ""):
                    return True
                length = int(user32.GetWindowTextLengthW(hwnd) or 0)
                title_buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title_buf, length + 1)
                title = title_buf.value or ""
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                rw = rect.right - rect.left
                rh = rect.bottom - rect.top
                if rw < 80 or rh < 60:
                    return True
                candidates.append((int(hwnd), rw * rh, title))
            except Exception:
                pass
            return True

        deadline = time.time() + 3.0
        while time.time() < deadline:
            candidates.clear()
            user32.EnumWindows(_enum, 0)
            # Prefer our viewer title ("Name · 3D") once it loads.
            titled = [c for c in candidates if "3D" in c[2] or "Sketchfab" in c[2] or "Preview" in c[2]]
            pool = titled or candidates
            if pool:
                break
            time.sleep(0.06)
        else:
            return False

        # Prefer titled viewer; among those, any size (we'll resize).
        best = max(pool, key=lambda t: (1 if "3D" in t[2] else 0, t[1]))[0]

        HWND_TOPMOST = -1
        SWP_SHOWWINDOW = 0x0040
        ok = bool(
            user32.SetWindowPos(
                best,
                HWND_TOPMOST,
                int(left),
                int(top),
                max(120, int(width)),
                max(100, int(height)),
                SWP_SHOWWINDOW,
            )
        )
        return ok
    except Exception:
        return False


def _restore_main_window() -> None:
    """Undo accidental minimize / ensure Flet stays visible behind the overlay."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        _ensure_win32_types()
        user32 = ctypes.windll.user32
        our_pid = os.getpid()
        SW_RESTORE = 9
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lp):  # noqa: N802
            try:
                proc = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc))
                if int(proc.value) != our_pid:
                    return True
                if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
                    return True
                if user32.GetWindow(hwnd, 4):
                    return True
                found.append(int(hwnd))
            except Exception:
                pass
            return True

        user32.EnumWindows(_enum, 0)
        for hwnd in found:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            # Keep main app below the TOPMOST preview, but not minimized.
            user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM? no - NOSIZE|NOMOVE|NOACTIVATE
            # HWND_NOTOPMOST = -2
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    except Exception:
        pass


def open_viewer_app(
    url: str,
    *,
    width: int = 280,
    height: int = 300,
    left: int | None = None,
    top: int | None = None,
) -> bool:
    """
    Launch or replace a Chrome/Edge --app window for the viewer.
    Uses a private user-data-dir so size isn't restored from a huge prior window,
    then forces the HWND rect on Windows (Chrome only) and keeps it TOPMOST.
    """
    global _proc, _last_url
    url = (url or "").strip()
    if not url:
        return False

    bins = _chrome_candidates()
    if not bins:
        try:
            if sys.platform == "win32":
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _last_url = url
            return True
        except Exception:
            return False

    if _proc is not None and _proc.poll() is None:
        try:
            _proc.terminate()
        except Exception:
            pass
        _proc = None
        time.sleep(0.05)

    w = max(120, int(width))
    h = max(100, int(height))
    _PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        bins[0],
        f"--app={url}",
        f"--user-data-dir={_PROFILE}",
        f"--window-size={w},{h}",
        "--disable-features=TranslateUI",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    if left is not None and top is not None:
        args.append(f"--window-position={int(left)},{int(top)}")

    try:
        _proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _last_url = url
    except Exception:
        return False

    if left is not None and top is not None and _proc.pid:
        pid = int(_proc.pid)

        def _pin() -> None:
            _restore_main_window()
            for wait in (0.1, 0.25, 0.5, 1.0):
                time.sleep(wait)
                _force_window_rect(pid, int(left), int(top), w, h)
                _restore_main_window()

        threading.Thread(target=_pin, daemon=True).start()
    else:
        threading.Thread(target=_restore_main_window, daemon=True).start()

    return True


def focus_or_reopen(url: str | None = None) -> bool:
    target = (url or _last_url or "").strip()
    if not target:
        return False
    return open_viewer_app(target)


def close_viewer_app() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        try:
            _proc.terminate()
        except Exception:
            pass
    _proc = None
