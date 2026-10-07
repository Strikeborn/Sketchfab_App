"""Lightweight timing helpers for Activity-log perf budgets (P2)."""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

# Set PERF_LOG=0 to silence; default on for desktop debugging.
_ENABLED = os.environ.get("PERF_LOG", "1").strip().lower() not in {"0", "false", "no", "off"}

InfoFn = Callable[[str], None]


def enabled() -> bool:
    return _ENABLED


@contextmanager
def span(label: str, *, info: InfoFn | None = None, quiet: bool = False) -> Iterator[dict]:
    """Time a block; store ms on ctx['ms']. Optionally emit one Activity line."""
    box: dict = {"ms": 0.0, "label": label}
    t0 = time.perf_counter()
    try:
        yield box
    finally:
        box["ms"] = (time.perf_counter() - t0) * 1000.0
        msg = f"⏱ {label}: {box['ms']:.0f} ms"
        logger.debug(msg)
        if _ENABLED and info and not quiet:
            try:
                info(msg)
            except Exception:
                pass


def mark(label: str, ms: float, *, info: InfoFn | None = None, quiet: bool = False) -> None:
    msg = f"⏱ {label}: {ms:.0f} ms"
    logger.debug(msg)
    if _ENABLED and info and not quiet:
        try:
            info(msg)
        except Exception:
            pass
