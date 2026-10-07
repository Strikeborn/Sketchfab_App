"""Viewport-aware LOD thumbnails: hi-res on screen, low-res nearby, unload far away."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import flet as ft

_THUMB_BG = "#0f172a"
# On-screen + ~1.5 viewports of buffer stay HD so scrolling into soft thumbs is rare.
_PAD_HI = 1.5
_PAD_LOW = 3.0
_UNLOAD_SRC = ""  # unused — off level keeps low-res instead
_SYNC_BURST = 96  # upgrade a full screen+buffer without pacing


def thumb_urls_from_row(row, low_key: str = "Thumbnail", hi_key: str = "Thumbnail HD") -> tuple[str, str]:
    low = _fmt(row.get(low_key))
    hi = _fmt(row.get(hi_key)) or low
    return low, hi


def _fmt(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"none", "nan", "<na>"} else s


def make_lod_thumb(
    low: str,
    hi: str,
    size: int = 72,
    *,
    prefer_hi: bool = False,
) -> tuple[ft.Container, ft.Image | None]:
    src = (hi or low) if prefer_hi else (low or hi)
    if not src:
        box = ft.Container(
            width=size,
            height=size,
            bgcolor=_THUMB_BG,
            border_radius=4,
            alignment=ft.alignment.center,
            content=ft.Icon(ft.Icons.IMAGE_NOT_SUPPORTED, size=28, color=ft.Colors.GREY_600),
        )
        return box, None

    img = ft.Image(
        src=src,
        width=size,
        height=size,
        fit=ft.ImageFit.COVER,
        border_radius=4,
        gapless_playback=True,
    )
    box = ft.Container(
        width=size,
        height=size,
        bgcolor=_THUMB_BG,
        border_radius=4,
        alignment=ft.alignment.center,
        content=img,
    )
    return box, img


def _safe_update(control: ft.Control, fallback: ft.Control | None = None) -> bool:
    try:
        if getattr(control, "page", None):
            control.update()
            return True
        if fallback is not None and getattr(fallback, "page", None):
            fallback.update()
            return True
    except (AssertionError, RuntimeError):
        pass
    return False


@dataclass
class LodEntry:
    img: ft.Image
    low: str
    hi: str
    y0: float
    y1: float
    level: str = "low"  # hi | low | off


@dataclass
class ViewportLod:
    """Track thumbs and sync quality to the scroll viewport."""

    entries: list[LodEntry] = field(default_factory=list)
    token: int = 0
    _last_scroll: float = 0.0
    _last_viewport: float = 800.0
    _sync_gen: int = 0

    def clear(self) -> None:
        self.entries.clear()
        self.token += 1
        self._sync_gen += 1

    def add(
        self,
        img: ft.Image | None,
        low: str,
        hi: str,
        y0: float,
        y1: float,
        *,
        level: str | None = None,
    ) -> None:
        if img is None:
            return
        low = low or hi
        hi = hi or low
        if not low and not hi:
            return
        start = level or "low"
        if start not in {"hi", "low", "off"}:
            start = "low"
        self.entries.append(LodEntry(img=img, low=low, hi=hi, y0=y0, y1=y1, level=start))

    def desired_level(self, entry: LodEntry, scroll: float, viewport: float) -> str:
        hi_pad = viewport * _PAD_HI
        top = scroll - hi_pad
        bot = scroll + viewport + hi_pad
        if entry.y1 >= top and entry.y0 <= bot:
            return "hi"
        # Stay on low-res when off-screen — never clear src (avoids red/broken pop-in).
        return "low"

    def _upgrade_priority(self, entry: LodEntry, scroll: float, viewport: float) -> float:
        """Higher = upgrade sooner. True on-screen overlap first, then nearer to viewport center."""
        vis_top = scroll
        vis_bot = scroll + viewport
        overlap = max(0.0, min(entry.y1, vis_bot) - max(entry.y0, vis_top))
        mid = (entry.y0 + entry.y1) * 0.5
        center = scroll + viewport * 0.5
        dist = abs(mid - center)
        # On-screen cards: huge base + more overlap + closer to center.
        if overlap > 0:
            return 1_000_000.0 + overlap * 10.0 - dist
        # Soft pad zone still above off-screen noise.
        return -dist

    def src_for(self, entry: LodEntry, level: str) -> str:
        if level == "hi":
            return entry.hi or entry.low
        return entry.low or entry.hi

    async def sync(
        self,
        scroll: float,
        viewport: float,
        root: ft.Control | None = None,
        *,
        token: int | None = None,
        delay: float = 0.001,
        allow_downgrade: bool = True,
    ) -> None:
        if token is not None and token != self.token:
            return
        self._last_scroll = scroll
        self._last_viewport = max(120.0, viewport or 800.0)
        gen = self._sync_gen
        # Prioritize on-screen upgrades first, then downgrades/unloads.
        upgrades: list[LodEntry] = []
        downgrades: list[LodEntry] = []
        for entry in self.entries:
            want = self.desired_level(entry, self._last_scroll, self._last_viewport)
            if want == entry.level:
                continue
            if want == "hi":
                upgrades.append(entry)
            elif allow_downgrade:
                downgrades.append(entry)

        upgrades.sort(
            key=lambda e: self._upgrade_priority(e, self._last_scroll, self._last_viewport),
            reverse=True,
        )

        for i, entry in enumerate(upgrades):
            if gen != self._sync_gen or (token is not None and token != self.token):
                return
            await self._apply(entry, "hi", root)
            # Burst visible upgrades; only pace the long tail.
            if i >= _SYNC_BURST:
                await asyncio.sleep(delay)

        for entry in downgrades:
            if gen != self._sync_gen or (token is not None and token != self.token):
                return
            want = self.desired_level(entry, self._last_scroll, self._last_viewport)
            if want == "hi":
                continue
            await self._apply(entry, want, root)
            await asyncio.sleep(delay * 0.5)

    async def _apply(self, entry: LodEntry, level: str, root: ft.Control | None) -> None:
        src = self.src_for(entry, level)
        new_src = src or ""
        if not new_src:
            return
        if (entry.img.src or "") == new_src and entry.level == level:
            return
        # Don't mark hi until the Image is mounted — otherwise a pre-mount kick
        # sets level=hi with no visible update, and later scroll syncs skip it.
        if getattr(entry.img, "page", None) is None and getattr(root, "page", None) is None:
            return
        entry.img.src = new_src
        if not _safe_update(entry.img, fallback=root):
            return
        entry.level = level


async def upgrade_thumbnails(
    images: list[tuple[ft.Image, str, str]],
    token: int,
    active_token: int,
    root: ft.Control | None = None,
    delay: float = 0.02,
    mount_wait: float = 0.06,
    max_upgrade: int = 48,
) -> None:
    """Legacy helper: upgrade first N thumbs to hi-res (no viewport unload)."""
    if not images:
        return
    await asyncio.sleep(mount_wait)
    visible = images[:max_upgrade]
    for i, (img, low, hi) in enumerate(visible):
        if token != active_token:
            return
        if not hi or hi == low or not img.src:
            continue
        if getattr(img, "page", None) is None:
            continue
        img.src = hi
        if not _safe_update(img, fallback=root):
            return
        if i >= _SYNC_BURST:
            await asyncio.sleep(delay)


class ScrollLodDebouncer:
    """Throttle viewport LOD sync while scrolling (not idle-only debounce)."""

    def __init__(
        self,
        lod: ViewportLod,
        root: ft.Control,
        wait_s: float = 0.03,
        throttle_s: float = 0.02,
    ):
        self.lod = lod
        self.root = root
        # wait_s = settle before downgrades; throttle = in-motion upgrade cadence.
        self.wait_s = wait_s
        self.throttle_s = max(0.012, float(throttle_s or wait_s or 0.02))
        self._scroll = 0.0
        self._viewport = 800.0
        self._dirty = False
        self._looping = False
        self._page: ft.Page | None = None
        self._paused = False

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    def on_scroll(self, e: ft.OnScrollEvent, page: ft.Page | None) -> None:
        try:
            self._scroll = float(e.pixels or 0)
            self._viewport = float(e.viewport_dimension or self._viewport or 800)
        except (TypeError, ValueError):
            return
        self._page = page
        self._dirty = True
        self._ensure_loop()

    def kick(
        self,
        page: ft.Page | None,
        scroll: float = 0.0,
        viewport: float = 800.0,
        *,
        remount_delay: float = 0.0,
    ) -> None:
        self._scroll = float(scroll or 0)
        self._viewport = float(viewport or self._viewport or 800)
        self._page = page
        self._dirty = True
        self._ensure_loop()
        # First paint often mounts Images one frame later — re-kick so HD pops in without scrolling.
        if remount_delay and page:
            try:
                page.run_task(self._delayed_kick, remount_delay)
            except Exception:
                pass

    async def _delayed_kick(self, delay: float) -> None:
        await asyncio.sleep(max(0.0, float(delay or 0)))
        self._dirty = True
        self._ensure_loop()

    def nudge(self, page: ft.Page | None, scroll: float, viewport: float | None = None) -> None:
        """Programmatic scroll (middle-drag) — same path as wheel, no OnScrollEvent needed."""
        self._scroll = float(scroll or 0)
        if viewport is not None:
            self._viewport = float(viewport or self._viewport or 800)
        self._page = page
        self._dirty = True
        self._ensure_loop()

    def sync_now(self, *, allow_downgrade: bool = False) -> None:
        """One-shot viewport LOD sync (window move / middle-drag)."""
        pg = self._page
        if not pg or self._paused:
            return

        async def _do() -> None:
            await self.lod.sync(
                self._scroll,
                self._viewport,
                self.root,
                token=self.lod.token,
                allow_downgrade=allow_downgrade,
                delay=0,
            )

        try:
            pg.run_task(_do)
        except Exception:
            pass

    def _ensure_loop(self) -> None:
        if self._looping or not self._page:
            return
        self._looping = True
        try:
            self._page.run_task(self._loop)
        except Exception:
            self._looping = False

    async def _loop(self) -> None:
        try:
            while True:
                if self._paused:
                    await asyncio.sleep(0.05)
                    if not self._dirty:
                        break
                    continue
                if not self._dirty:
                    # Trailing settle: upgrades+downgrades once scroll stops.
                    await asyncio.sleep(self.wait_s)
                    if not self._dirty:
                        await self.lod.sync(
                            self._scroll,
                            self._viewport,
                            self.root,
                            token=self.lod.token,
                            allow_downgrade=True,
                        )
                        break
                self._dirty = False
                # While scrolling: only upgrade — never blur on-screen/buffer thumbs.
                await self.lod.sync(
                    self._scroll,
                    self._viewport,
                    self.root,
                    token=self.lod.token,
                    allow_downgrade=False,
                )
                await asyncio.sleep(self.throttle_s)
        finally:
            self._looping = False
            if self._dirty and self._page:
                self._ensure_loop()
