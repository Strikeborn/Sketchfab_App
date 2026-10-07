"""Activity panel — short messages + in-place progress bars (no spam lines)."""
from __future__ import annotations

import re
import time

import flet as ft

from ui.scroll_drag import wrap_middle_drag_scroll

_MAX_LINES = 80

# Iterative status lines that should replace the previous matching line.
_REPLACE_RE = re.compile(
    r"^(?:"
    r"Likes\b|"
    r"Listing collections\b|"
    r"Subscriptions\b|"
    r"Checking collections\b|"
    r"Fetched likes page\b|"
    r"Fetched collections page\b|"
    r"Fetched subscriptions page\b|"
    r"\s*collection \d+/\d+:|"
    r"Mapping collections\b|"
    r"Writing workbook\b|"
    r"Checking\b|"
    r"Sync check\b|"
    r"Push\b|"
    r"Liked model\b|"
    r"Already liked\b"
    r")",
    re.IGNORECASE,
)


def _replace_family(line: str) -> str | None:
    """Return a stable key if this line should overwrite the previous same-family line."""
    s = (line or "").strip()
    if not s:
        return None
    if _REPLACE_RE.search(s):
        low = s.lower()
        if low.startswith("likes…") or low.startswith("likes..."):
            return "likes-page"
        if "likes page" in low:
            return "likes-page"
        if low.startswith("listing collections"):
            return "colls-page"
        if "collections page" in low:
            return "colls-page"
        if low.startswith("subscriptions"):
            return "subs-page"
        if low.startswith("checking collections"):
            return "coll-map"
        if re.match(r"^\s*collection \d+/\d+:", s, re.I):
            return "coll-map"
        if low.startswith("mapping"):
            return "coll-map"
        if low.startswith("writing workbook"):
            return "write-wb"
        if low.startswith("checking") or low.startswith("sync"):
            return "sync"
        if low.startswith("liked model") or low.startswith("already liked"):
            return "like"
        return "iter"
    return None


class LogView(ft.Column):
    def __init__(self):
        self._text = ft.Text("", selectable=True, size=11, color=ft.Colors.GREY_300)
        self._jobs: dict[str, dict] = {}
        self._jobs_col = ft.Column(spacing=6, tight=True)
        self._log_col = ft.Column(
            [self._jobs_col, self._text],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=8,
        )
        self._log_wrap = wrap_middle_drag_scroll(self._log_col, expand=True)
        self._dirty = False
        self._last_family: str | None = None
        self._job_t0: dict[str, float] = {}
        super().__init__(
            expand=True,
            spacing=4,
            controls=[
                ft.Text("Activity", weight=ft.FontWeight.BOLD, size=12),
                ft.Container(
                    expand=True,
                    bgcolor="#0f172a",
                    border_radius=6,
                    padding=8,
                    border=ft.border.all(1, "#334155"),
                    alignment=ft.alignment.top_left,
                    # Scroll area must be the sole expand child — a spacer sibling
                    # steals half the height and clips the scrollbar halfway down.
                    content=self._log_wrap,
                ),
            ],
        )

    def append(self, line: str, *, sync: bool = True, replace: bool | None = None):
        line = (line or "").rstrip()
        if not line:
            return
        prev = self._text.value or ""
        last = prev.rsplit("\n", 1)[-1] if prev else ""
        if last and last == line:
            return

        family = _replace_family(line)
        do_replace = replace if replace is not None else bool(family)
        if do_replace and family and family == self._last_family and prev:
            # Overwrite last line in-place.
            head, _, _old = prev.rpartition("\n")
            merged = (head + "\n" + line) if head else line
        else:
            merged = (prev + ("\n" if prev else "") + line)

        self._last_family = family if do_replace else None
        parts = merged.split("\n")
        if len(parts) > _MAX_LINES:
            merged = "\n".join(parts[-_MAX_LINES:])
        self._text.value = merged
        if not sync:
            self._dirty = True
            return
        self._dirty = False
        if self.page:
            self.update()

    def flush(self):
        if self._dirty and self.page:
            self._dirty = False
            self.update()

    def set(self, text: str):
        self._text.value = text or ""
        self._last_family = None
        self._dirty = False
        if self.page:
            self.update()

    def progress_start(self, key: str, label: str, total: int = 0) -> None:
        key = key or "job"
        self._job_t0[key] = time.monotonic()
        bar = ft.ProgressBar(value=0 if total > 0 else None, bar_height=6, bgcolor="#1e293b", color="#38bdf8")
        title = ft.Text(label or key, size=11, weight=ft.FontWeight.W_500, color=ft.Colors.GREY_200)
        status = ft.Text("starting…", size=10, color=ft.Colors.GREY_500)
        box = ft.Container(
            bgcolor="#111827",
            border=ft.border.all(1, "#334155"),
            border_radius=6,
            padding=8,
            content=ft.Column([title, bar, status], spacing=4, tight=True),
        )
        self._jobs[key] = {
            "box": box,
            "bar": bar,
            "title": title,
            "status": status,
            "total": max(0, int(total or 0)),
            "label": label or key,
        }
        self._rebuild_jobs()
        if self.page:
            self.update()

    def _elapsed(self, key: str) -> str:
        t0 = self._job_t0.get(key)
        if t0 is None:
            return ""
        sec = max(0.0, time.monotonic() - t0)
        if sec < 60:
            return f"{sec:.0f}s"
        m = int(sec // 60)
        s = int(sec % 60)
        return f"{m}m {s:02d}s"

    def progress_update(self, key: str, done: int, total: int | None = None, msg: str = "") -> None:
        key = key or "job"
        job = self._jobs.get(key)
        if not job:
            self.progress_start(key, msg or "Working…", total or 0)
            job = self._jobs[key]
        tot = int(total if total is not None else job["total"] or 0)
        elapsed = self._elapsed(key)
        base = msg or (f"{done:,} / {tot:,}" if tot > 0 else f"{done:,}…")
        timed = f"{base}  ·  {elapsed}" if elapsed else base
        if tot > 0:
            job["total"] = tot
            job["bar"].value = max(0.0, min(1.0, float(done) / float(tot)))
            job["status"].value = timed
        else:
            job["bar"].value = None  # indeterminate
            job["status"].value = timed
        if self.page:
            try:
                job["box"].update()
            except Exception:
                self.update()

    def progress_done(self, key: str, summary: str = "") -> None:
        key = key or "job"
        elapsed = self._elapsed(key)
        self._job_t0.pop(key, None)
        job = self._jobs.pop(key, None)
        if job:
            job["bar"].value = 1.0
            if summary:
                if elapsed and "·" not in summary and " in " not in summary.lower():
                    summary = f"{summary}  ·  {elapsed}"
                self.append(summary, replace=False)
            self._rebuild_jobs()
            if self.page:
                self.update()
        elif summary:
            self.append(summary, replace=False)

    def _rebuild_jobs(self) -> None:
        self._jobs_col.controls = [j["box"] for j in self._jobs.values()]
