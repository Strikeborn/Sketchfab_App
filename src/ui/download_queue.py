"""Bottom-right download queue — sequential or parallel GLB downloads without locking the UI."""
from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import flet as ft

from sketchfab_client import SketchfabClient, humanize_sketchfab_error

_BG = "#0f172a"
_MUTED = "#94a3b8"
_TEXT = "#e2e8f0"
_ACCENT = "#38bdf8"
_OK = "#4ade80"
_ERR = "#f87171"

DownloadFn = Callable[..., Path]  # (uid, name, dest_dir, link_dirs=..., on_progress=...)

_active_download_jobs = 0
_active_jobs_lock = threading.Lock()


def downloads_busy() -> bool:
    """True while any GLB download is resolving URL or transferring bytes."""
    with _active_jobs_lock:
        return _active_download_jobs > 0


def _fmt_bytes(n: float | int) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "?"
    if v < 0:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    for u in units:
        if v < 1024.0 or u == units[-1]:
            if u == "B":
                return f"{int(v)} {u}"
            return f"{v:.1f} {u}"
        v /= 1024.0
    return f"{v:.1f} TB"


def _fmt_speed(bps: float) -> str:
    try:
        v = float(bps)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    return f"{_fmt_bytes(v)}/s"


@dataclass
class _Job:
    uid: str
    name: str
    dest_dir: Path | None = None
    link_dirs: list[Path] = field(default_factory=list)
    author: str = ""
    license: str = ""
    status: str = "queued"  # queued | active | done | error
    detail: str = ""
    path: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    speed_now: float = 0.0
    speed_avg: float = 0.0


class DownloadQueuePane(ft.Column):
    """Shows active + queued downloads; workers run off the UI thread."""

    def __init__(
        self,
        *,
        on_complete: Callable[[str, str, Path | None, str | None], None] | None = None,
        on_organize: Callable[[], None] | None = None,
    ):
        self._on_complete = on_complete
        self._on_organize = on_organize
        self._download_fn: DownloadFn | None = None
        self._dest_dir: Path | None = None
        self._queue: deque[_Job] = deque()
        self._active: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._workers = 1
        self._pool: ThreadPoolExecutor | None = None
        self._shutdown = False
        self._organizing = False
        self._last_paint = 0.0
        self._title = ft.Text("Downloads", weight=ft.FontWeight.BOLD, size=12)
        self._status = ft.Text("Idle", size=10, color=_MUTED)
        self._organize_btn = ft.TextButton(
            "Organize",
            icon=ft.Icons.CREATE_NEW_FOLDER,
            tooltip="Move downloads into Collection/Title - Author - License/ folders (only folders that have files)",
            style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=2)),
            on_click=lambda e: self._click_organize(),
        )
        self._parallel = ft.Slider(
            min=1,
            max=4,
            divisions=3,
            value=1,
            label="{value}",
            width=100,
            tooltip="1 = one-by-one · higher = simultaneous downloads",
            on_change=self._on_parallel_change,
        )
        self._list = ft.Column(spacing=4, tight=True, scroll=ft.ScrollMode.AUTO, expand=True)
        super().__init__(
            expand=True,
            spacing=4,
            controls=[
                ft.Row(
                    [
                        self._title,
                        self._organize_btn,
                        ft.Container(expand=True),
                        ft.Text("Parallel", size=10, color=_MUTED),
                        self._parallel,
                    ],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self._status,
                ft.Container(
                    expand=True,
                    bgcolor=_BG,
                    border_radius=6,
                    padding=6,
                    border=ft.border.all(1, "#334155"),
                    alignment=ft.alignment.top_left,
                    content=self._list,
                ),
            ],
        )

    def set_organize_callback(self, on_organize: Callable[[], None] | None) -> None:
        self._on_organize = on_organize

    def set_organizing(self, busy: bool, detail: str = "") -> None:
        self._organizing = bool(busy)
        self._organize_btn.disabled = self._organizing
        if self._organizing:
            self._status.value = detail or "Organizing into collection folders…"
            self._status.color = _ACCENT
        else:
            self._status.color = _MUTED
            if not self._active and not self._queue:
                self._status.value = detail or "Idle"
            elif detail:
                self._status.value = detail
        if self.page:
            try:
                self.update()
            except Exception:
                pass

    def _click_organize(self) -> None:
        if self._organizing or not self._on_organize:
            return
        self._on_organize()

    def configure(
        self,
        *,
        download_fn: DownloadFn,
        dest_dir: Path,
        on_complete: Callable[[str, str, Path | None, str | None], None] | None = None,
    ) -> None:
        self._download_fn = download_fn
        self._dest_dir = Path(dest_dir)
        if on_complete is not None:
            self._on_complete = on_complete

    def shutdown(self) -> None:
        """Cancel queued downloads and stop workers (app exit)."""
        self._shutdown = True
        with self._lock:
            self._queue.clear()
        pool = self._pool
        self._pool = None
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        with self._lock:
            self._active.clear()

    def enqueue(
        self,
        uid: str,
        name: str,
        *,
        dest_dir: Path | None = None,
        link_dirs: list[Path] | None = None,
        author: str = "",
        license: str = "",
    ) -> bool:
        uid = str(uid or "").strip()
        if not uid or not self._download_fn or not (dest_dir or self._dest_dir):
            return False
        with self._lock:
            if uid in self._active or any(j.uid == uid for j in self._queue):
                return False
            self._queue.append(
                _Job(
                    uid=uid,
                    name=name or uid,
                    dest_dir=Path(dest_dir) if dest_dir else None,
                    link_dirs=[Path(p) for p in (link_dirs or []) if p],
                    author=author or "",
                    license=license or "",
                )
            )
        SketchfabClient.reset_download_lane()
        self._paint()
        self._pump()
        return True

    def _on_parallel_change(self, e) -> None:
        try:
            self._workers = max(1, min(4, int(round(float(e.control.value or 1)))))
        except (TypeError, ValueError):
            self._workers = 1
        self._status.value = (
            f"Mode: one-by-one" if self._workers == 1 else f"Mode: up to {self._workers} at once"
        )
        try:
            self._status.update()
        except Exception:
            pass
        self._pump()

    def _ensure_pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="glb-dl")
        return self._pool

    def _pump(self) -> None:
        if self._shutdown:
            return
        with self._lock:
            while len(self._active) < self._workers and self._queue:
                job = self._queue.popleft()
                job.status = "active"
                job.detail = "starting…"
                self._active[job.uid] = job
                self._ensure_pool().submit(self._run_job, job)
        self._paint()

    def _job_progress(self, job: _Job, done: int, total: int, speed_now: float, speed_avg: float) -> None:
        job.bytes_done = max(0, int(done or 0))
        job.bytes_total = max(0, int(total or 0))
        job.speed_now = max(0.0, float(speed_now or 0))
        job.speed_avg = max(0.0, float(speed_avg or 0))
        if job.bytes_total > 0:
            pct = min(100.0, 100.0 * job.bytes_done / job.bytes_total)
            job.detail = (
                f"{_fmt_bytes(job.bytes_done)} / {_fmt_bytes(job.bytes_total)} ({pct:.0f}%)"
            )
        else:
            job.detail = f"{_fmt_bytes(job.bytes_done)} downloaded"
        # Throttle UI paint so we don't flood Flet from the download thread.
        now = time.monotonic()
        if now - self._last_paint >= 0.2:
            self._last_paint = now
            self._paint()

    def _run_job(self, job: _Job) -> None:
        if self._shutdown:
            return
        global _active_download_jobs
        with _active_jobs_lock:
            _active_download_jobs += 1
        err: str | None = None
        path: Path | None = None
        try:
            assert self._download_fn
            dest = job.dest_dir or self._dest_dir
            assert dest is not None
            job.detail = "Resolving download URL…"
            self._paint()
            path = self._download_fn(
                job.uid,
                job.name,
                dest,
                link_dirs=job.link_dirs or None,
                author=job.author,
                license=job.license,
                on_progress=lambda d, t, sn, sa, j=job: self._job_progress(j, d, t, sn, sa),
            )
            job.status = "done"
            job.path = str(path)
            size = job.bytes_total or job.bytes_done
            try:
                leaf = f"{path.parent.parent.name}/{path.parent.name}" if path else "done"
            except Exception:
                leaf = path.name if path else "done"
            size_bit = f" · {_fmt_bytes(size)}" if size else ""
            avg_bit = f" · avg {_fmt_speed(job.speed_avg)}" if job.speed_avg > 0 else ""
            job.detail = f"{leaf}{size_bit}{avg_bit}"
        except Exception as exc:
            err = humanize_sketchfab_error(exc)
            job.status = "error"
            job.detail = err[:80]
        finally:
            with _active_jobs_lock:
                _active_download_jobs = max(0, _active_download_jobs - 1)
        with self._lock:
            self._active.pop(job.uid, None)
        self._paint()
        if self._shutdown:
            return
        if self._on_complete:
            try:
                self._on_complete(job.uid, job.name, path, err)
            except Exception:
                pass
        self._pump()

    def _paint(self) -> None:
        with self._lock:
            active = list(self._active.values())
            queued = list(self._queue)
        rows: list[ft.Control] = []
        for j in active:
            rows.append(self._row(j, color=_ACCENT, icon=ft.Icons.DOWNLOADING))
        for j in queued[:8]:
            rows.append(self._row(j, color=_MUTED, icon=ft.Icons.SCHEDULE))
        if len(queued) > 8:
            rows.append(ft.Text(f"+{len(queued) - 8} more in queue…", size=10, color=_MUTED))
        if not rows:
            rows.append(ft.Text("No downloads — queue is empty.", size=10, color=_MUTED))
        n_a, n_q = len(active), len(queued)
        if n_a or n_q:
            mode = "one-by-one" if self._workers == 1 else f"{self._workers} parallel"
            self._status.value = f"{n_a} active · {n_q} queued · {mode}"
        else:
            self._status.value = "Idle"
        self._list.controls = rows
        if self.page:
            try:
                self.update()
            except Exception:
                pass

    def _row(self, job: _Job, *, color: str, icon) -> ft.Control:
        label = job.name or job.uid
        if len(label) > 32:
            label = label[:30] + "…"

        if job.status == "active":
            total = job.bytes_total
            done = job.bytes_done
            value = (done / total) if total > 0 else None
            if total > 0:
                size_line = f"{_fmt_bytes(done)} / {_fmt_bytes(total)}"
            elif done > 0:
                size_line = f"{_fmt_bytes(done)} / ?"
            else:
                size_line = "fetching…"
            speed_line = (
                f"cur {_fmt_speed(job.speed_now)} · avg {_fmt_speed(job.speed_avg)}"
                if (job.speed_now > 0 or job.speed_avg > 0)
                else "…"
            )
            bar = ft.ProgressBar(
                value=value,
                color=_ACCENT,
                bgcolor="#1e293b",
                bar_height=4,
                expand=True,
            )
            return ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(icon, size=12, color=color),
                            ft.Text(
                                label,
                                size=10,
                                color=_TEXT,
                                expand=True,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bar,
                    ft.Row(
                        [
                            ft.Text(size_line, size=9, color=_MUTED, expand=True),
                            ft.Text(speed_line, size=9, color=_MUTED),
                        ],
                        spacing=4,
                    ),
                ],
                spacing=2,
                tight=True,
            )

        detail_color = _OK if job.status == "done" else (_ERR if job.status == "error" else _MUTED)
        return ft.Row(
            [
                ft.Icon(icon, size=12, color=color),
                ft.Text(
                    label,
                    size=10,
                    color=_TEXT,
                    expand=True,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Text(
                    job.detail or job.status,
                    size=9,
                    color=detail_color,
                    width=140,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
