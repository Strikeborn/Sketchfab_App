"""Append vetted tags into collections_terms.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

_DEFAULT = Path("terms/collections_terms.yaml")


def append_tag_to_collection(term: str, collection: str, path: str | Path | None = None) -> bool:
    t = str(term).strip().lower()
    c = str(collection).strip()
    if not t or not c:
        return False
    p = Path(path or _DEFAULT)
    if not p.exists():
        return False
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    colls = data.setdefault("collections", {})
    entry = colls.setdefault(c, {"include_terms": [], "tag_terms": [], "exclude_terms": [], "fuzzy_threshold": 88})
    tags = entry.setdefault("tag_terms", [])
    includes = entry.setdefault("include_terms", [])
    changed = False
    if t not in [x.lower() for x in tags]:
        tags.append(t)
        changed = True
    if t not in [x.lower() for x in includes]:
        includes.append(t)
        changed = True
    if changed:
        p.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return changed
