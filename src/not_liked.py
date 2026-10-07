"""Track GLB downloads for models that are not (yet) in Liked."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

NOTLIKED_FOLDER = "NotLiked"
_PATH = Path("data") / "not_liked_downloads.json"
_LOCK = threading.Lock()


def _load() -> dict:
    if not _PATH.is_file():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def is_tracked(uid: str) -> bool:
    uid = (uid or "").strip().lower()
    if not uid:
        return False
    with _LOCK:
        return uid in _load()


def tracked_uids() -> set[str]:
    with _LOCK:
        return set(_load().keys())


def record_download(
    uid: str,
    *,
    name: str = "",
    path: str = "",
    author: str = "",
) -> None:
    uid = (uid or "").strip().lower()
    if not uid:
        return
    with _LOCK:
        data = _load()
        data[uid] = {
            "name": (name or "").strip(),
            "path": str(path or ""),
            "author": (author or "").strip(),
            "downloaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "status": "not_liked",
        }
        _save(data)


def mark_liked(uid: str) -> None:
    """Model was liked — keep the record but flip status (folder may still be NotLiked until Organize)."""
    uid = (uid or "").strip().lower()
    if not uid:
        return
    with _LOCK:
        data = _load()
        row = data.get(uid)
        if not row:
            return
        row["status"] = "liked"
        row["liked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _save(data)


def get_entry(uid: str) -> dict | None:
    uid = (uid or "").strip().lower()
    if not uid:
        return None
    with _LOCK:
        row = _load().get(uid)
        return dict(row) if row else None
