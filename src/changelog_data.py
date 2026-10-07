from __future__ import annotations

import os
from pathlib import Path

import yaml

from version import APP_VERSION

_DEFAULT_PATH = Path("data") / "changelog.yaml"


def changelog_path() -> Path:
    env = os.environ.get("CHANGELOG_PATH", "").strip()
    return Path(env) if env else _DEFAULT_PATH


def load_changelog() -> dict:
    path = changelog_path()
    if not path.exists():
        return {"version": APP_VERSION, "entries": []}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("version", APP_VERSION)
    data.setdefault("entries", [])
    data.setdefault("app", {})
    return data
