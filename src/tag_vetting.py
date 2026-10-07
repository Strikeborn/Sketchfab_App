"""Persist approved/rejected tags for assignment vetting."""
from __future__ import annotations

from pathlib import Path

import yaml

_VET_PATH = Path("data/tag_vetting.yaml")


def _load() -> dict:
    if not _VET_PATH.exists():
        return {"approved": [], "rejected": []}
    try:
        data = yaml.safe_load(_VET_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        return {"approved": [], "rejected": []}
    return {
        "approved": sorted({str(t).strip().lower() for t in (data.get("approved") or []) if str(t).strip()}),
        "rejected": sorted({str(t).strip().lower() for t in (data.get("rejected") or []) if str(t).strip()}),
    }


def _save(data: dict) -> None:
    _VET_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "approved": sorted(data.get("approved") or []),
        "rejected": sorted(data.get("rejected") or []),
    }
    _VET_PATH.write_text(yaml.dump(out, default_flow_style=False, allow_unicode=True), encoding="utf-8")


def get_vetting() -> dict:
    return _load()


def vet_term(term: str, status: str) -> None:
    """status: 'approved' | 'rejected' | 'clear'"""
    t = str(term).strip().lower()
    if not t:
        return
    data = _load()
    for key in ("approved", "rejected"):
        data[key] = [x for x in data[key] if x != t]
    if status == "approved":
        data["approved"].append(t)
    elif status == "rejected":
        data["rejected"].append(t)
    _save(data)


def term_status(term: str) -> str | None:
    t = str(term).strip().lower()
    data = _load()
    if t in data["approved"]:
        return "approved"
    if t in data["rejected"]:
        return "rejected"
    return None


def filter_term_items(items: list[tuple[str, int]], mode: str) -> list[tuple[str, int]]:
    """mode: all | unvetted | approved | rejected"""
    if mode == "all":
        return items
    data = _load()
    approved = set(data["approved"])
    rejected = set(data["rejected"])
    out: list[tuple[str, int]] = []
    for term, count in items:
        tl = term.lower()
        st = "approved" if tl in approved else ("rejected" if tl in rejected else None)
        if mode == "unvetted" and st is None:
            out.append((term, count))
        elif mode == "approved" and st == "approved":
            out.append((term, count))
        elif mode == "rejected" and st == "rejected":
            out.append((term, count))
    return out
