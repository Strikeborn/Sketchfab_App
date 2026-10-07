from __future__ import annotations

import os
from pathlib import Path

import yaml

_DEFAULT_PATH = Path("data") / "browse_presets.yaml"


def presets_path() -> Path:
    env = os.environ.get("BROWSE_PRESETS_PATH", "").strip()
    return Path(env) if env else _DEFAULT_PATH


def load_presets() -> list[dict]:
    path = presets_path()
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    presets = data.get("presets") or []
    return [p for p in presets if isinstance(p, dict) and p.get("name")]


def save_presets(presets: list[dict]) -> Path:
    path = presets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"presets": presets}, f, default_flow_style=False, allow_unicode=True)
    return path


def add_preset(preset: dict) -> list[dict]:
    presets = load_presets()
    name = str(preset.get("name", "")).strip()
    presets = [p for p in presets if str(p.get("name", "")).strip() != name]
    presets.insert(0, preset)
    save_presets(presets)
    return presets
