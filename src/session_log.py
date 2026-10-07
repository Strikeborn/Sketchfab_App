"""Persistent session log — Activity, logging, stdout/stderr → data/app.log."""
from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()
_log_path: Path | None = None
_tee_installed = False
_MAX_BYTES = 2 * 1024 * 1024


def log_file(root: str | Path | None = None) -> Path:
    global _log_path
    if _log_path is None:
        base = Path(root or os.getcwd())
        data = base / "data"
        data.mkdir(parents=True, exist_ok=True)
        _log_path = data / "app.log"
    return _log_path


def _trim_if_huge(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            text = path.read_text(encoding="utf-8", errors="replace")
            keep = text[-(_MAX_BYTES // 2) :]
            path.write_text("...(log trimmed)...\n" + keep, encoding="utf-8")
    except OSError:
        pass


def _append_raw(line: str) -> None:
    if _log_path is None:
        return
    s = (line or "").rstrip("\r\n")
    if not s:
        return
    with _lock:
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(s + "\n")
                f.flush()
        except OSError:
            pass


def activity(msg: str) -> None:
    """User-facing Activity panel lines."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _append_raw(f"{ts} ACTIVITY {msg}")


class _StreamTee:
    """Mirror stdout/stderr to the session log (Flet tracebacks, print(), etc.)."""

    def __init__(self, stream, prefix: str):
        self._stream = stream
        self._prefix = prefix
        self._buf = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        try:
            self._stream.write(data)
            self._stream.flush()
        except Exception:
            pass
        self._buf += data.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                _append_raw(f"{ts} {self._prefix} {line}")
        return len(data)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass
        rest = self._buf.strip()
        if rest:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            _append_raw(f"{ts} {self._prefix} {rest}")
            self._buf = ""

    def isatty(self) -> bool:
        fn = getattr(self._stream, "isatty", None)
        return bool(fn()) if callable(fn) else False

    def fileno(self) -> int:
        return self._stream.fileno()


def setup_session_log(root: str | Path, *, version: str = "") -> Path:
    """Configure file+console logging and tee stdout/stderr. Call once at app startup."""
    global _tee_installed
    path = log_file(root)
    _trim_if_huge(path)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    banner = f"{'=' * 60}\nSESSION START {ts}"
    if version:
        banner += f"  v{version}"
    _append_raw(banner)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.__stderr__)
    sh.setFormatter(fmt)
    root_logger.addHandler(fh)
    root_logger.addHandler(sh)

    if not _tee_installed:
        sys.stdout = _StreamTee(sys.__stdout__, "STDOUT")
        sys.stderr = _StreamTee(sys.__stderr__, "STDERR")
        _tee_installed = True

    def _excepthook(exc_type, exc, tb):
        import traceback

        text = "".join(traceback.format_exception(exc_type, exc, tb))
        _append_raw(f"UNCAUGHT EXCEPTION:\n{text}")
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook
    logging.getLogger(__name__).info("Session log → %s", path)
    return path


def end_session(*, version: str = "") -> None:
    """Append session footer and flush (safe to call right before os._exit)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"SESSION END {ts}"
    if version:
        line += f"  v{version}"
    _append_raw(line)
    _append_raw("=" * 60)
