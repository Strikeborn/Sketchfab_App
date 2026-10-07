"""Per-model dismiss of collection suggestions (Liked chips X)."""
from __future__ import annotations

from pathlib import Path

import yaml

_PATH = Path("data") / "suggestion_dismiss.yaml"
_CACHE: dict[str, list[str]] | None = None


def _load() -> dict[str, list[str]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not _PATH.exists():
        _CACHE = {}
        return _CACHE
    try:
        raw = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        _CACHE = {}
        return _CACHE
    out: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for uid, names in raw.items():
            key = str(uid).strip()
            if not key:
                continue
            cleaned = []
            seen: set[str] = set()
            for n in names or []:
                name = str(n).strip()
                low = name.casefold()
                if name and low not in seen:
                    cleaned.append(name)
                    seen.add(low)
            if cleaned:
                out[key] = cleaned
    _CACHE = out
    return _CACHE


def _save(data: dict[str, list[str]]) -> None:
    global _CACHE
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    _CACHE = data


def list_for(uid: str) -> list[str]:
    return list(_load().get(str(uid).strip(), []))


def is_dismissed(uid: str, name: str) -> bool:
    want = str(name).strip().casefold()
    if not want:
        return False
    return any(n.casefold() == want for n in list_for(uid))


def dismiss(uid: str, name: str) -> None:
    u = str(uid).strip()
    n = str(name).strip()
    if not u or not n:
        return
    data = dict(_load())
    cur = list(data.get(u, []))
    if any(x.casefold() == n.casefold() for x in cur):
        return
    cur.append(n)
    data[u] = cur
    _save(data)


def clear_dismiss(uid: str, name: str | None = None) -> None:
    u = str(uid).strip()
    if not u:
        return
    data = dict(_load())
    if u not in data:
        return
    if name is None:
        data.pop(u, None)
    else:
        want = str(name).strip().casefold()
        data[u] = [x for x in data[u] if x.casefold() != want]
        if not data[u]:
            data.pop(u, None)
    _save(data)


def reload_cache() -> None:
    global _CACHE
    _CACHE = None
    _load()
