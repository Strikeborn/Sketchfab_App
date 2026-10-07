"""Build public Sketchfab collection URLs (username/slug/uid format)."""
from __future__ import annotations

import os
import re

_cached_username: str | None = None


def slugify(name: str) -> str:
    s = (name or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "collection"


def sketchfab_username() -> str:
    global _cached_username
    if _cached_username is not None:
        return _cached_username
    env = os.environ.get("SKETCHFAB_USERNAME", "").strip()
    if env:
        _cached_username = env
        return env
    try:
        from sketchfab_client import SketchfabClient
        me = SketchfabClient().get_me()
        _cached_username = (me.get("username") or me.get("displayName") or "").strip()
    except Exception:
        _cached_username = ""
    return _cached_username


def set_sketchfab_username(username: str) -> None:
    global _cached_username
    _cached_username = (username or "").strip()


def collection_public_url(
    name: str,
    uid: str,
    *,
    slug: str | None = None,
    username: str | None = None,
) -> str:
    """e.g. https://sketchfab.com/<username>/collections/stairs-{uid}"""
    u = (username or sketchfab_username()).strip()
    uid = (uid or "").strip()
    if not uid:
        return ""
    sl = (slug or "").strip()
    if not sl:
        sl = f"{slugify(name)}-{uid}"
    elif uid not in sl:
        sl = f"{sl}-{uid}" if not sl.endswith(uid) else sl
    if u:
        return f"https://sketchfab.com/{u}/collections/{sl}"
    return f"https://sketchfab.com/search?q={uid}&type=collections"
