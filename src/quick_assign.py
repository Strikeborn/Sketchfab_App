"""Quick-assign keys → Sketchfab collection names (data/quick_assign.yaml)."""
from __future__ import annotations

from pathlib import Path

import yaml

from collection_match import match_collection_name

_DEFAULT_PATH = Path("data") / "quick_assign.yaml"
_DEFAULTS = {
    "F": "Female",
    "M": "Male",
    "N": "N Collection",
    "S": "Scenes",
    "E": "Enemy",
    "P": "Props",
    "FOODS": "Foods",
}
_ALLOWED_KEYS = frozenset(_DEFAULTS) | {"FOODS", "FOOD"}


def load_quick_assign() -> dict[str, str]:
    path = _DEFAULT_PATH
    out = dict(_DEFAULTS)
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for key, val in (data.get("assign") or data).items():
                k = str(key).strip().upper()
                v = str(val).strip()
                if not v:
                    continue
                if k in _ALLOWED_KEYS or k in {"F", "M", "N", "S", "E", "P", "FOODS", "FOOD"}:
                    if k == "FOOD":
                        k = "FOODS"
                    out[k] = v
        except OSError:
            pass
    return out


def resolve_collection(letter: str, collection_names: list[str]) -> str:
    """Map a quick-assign key to a collection name (exact / singular-plural tolerant)."""
    key = (letter or "").strip().upper()
    if key == "FOOD":
        key = "FOODS"
    target = load_quick_assign().get(key, "")
    if not target:
        return ""
    hit = match_collection_name(target, collection_names)
    return hit or target
