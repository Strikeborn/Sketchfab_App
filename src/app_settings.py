"""Persistent app settings (data/app_settings.yaml)."""
from __future__ import annotations

from pathlib import Path

import yaml

_PATH = Path("data") / "app_settings.yaml"

DEFAULTS: dict = {
    "dry_run_default": False,
    "background_sync_enabled": True,
    "background_sync_interval_sec": 60,
    "minimize_to_tray": True,
    "close_to_tray": False,
    "auto_quick_collect_on_drift": False,
    "quick_collect_max_like_pages": 20,
    "notify_on_sync_drift": True,
    "custom_window_caption": False,
    "dual_close_buttons": True,
    "hide_console_on_tray": True,
    # Author collection scan: extra collection search queries, and aliases per
    # author name ("*" applies to every scan). Personal values live in data/.
    "author_scan_queries": [],
    "author_scan_aliases": {},
    # Known model-name series prefixes for series discovery (e.g. "Studio Series").
    "series_prefixes": [],
}


def load_settings() -> dict:
    out = dict(DEFAULTS)
    if not _PATH.is_file():
        return out
    try:
        raw = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in DEFAULTS:
                    out[k] = v
            # Legacy: custom_window_caption → dual_close_buttons
            if "dual_close_buttons" not in raw and raw.get("custom_window_caption"):
                out["dual_close_buttons"] = True
    except (OSError, yaml.YAMLError):
        pass
    try:
        out["background_sync_interval_sec"] = max(
            15, int(out.get("background_sync_interval_sec") or 60)
        )
    except (TypeError, ValueError):
        out["background_sync_interval_sec"] = 60
    return out


def author_scan_config(author: str) -> tuple[list[str], list[str]]:
    """(collection queries, author aliases) for an author scan, from settings."""
    s = load_settings()
    queries = [str(q).strip() for q in (s.get("author_scan_queries") or []) if str(q).strip()]
    raw = s.get("author_scan_aliases") or {}
    aliases: list[str] = []
    if isinstance(raw, dict):
        want = (author or "").strip().casefold()
        for k, vals in raw.items():
            if str(k) == "*" or str(k).strip().casefold() == want:
                aliases.extend(str(v).strip() for v in (vals or []) if str(v).strip())
    aliases = [a for a in dict.fromkeys(aliases) if a.casefold() != (author or "").strip().casefold()]
    return queries, aliases


def save_settings(data: dict) -> None:
    merged = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in data:
            merged[k] = data[k]
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
