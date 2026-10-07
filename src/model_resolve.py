"""Resolve a public model UID from title ± author (deleted-account recovery)."""
from __future__ import annotations

import re

from sketchfab_client import SketchfabClient

_MAX_USER_PAGES = 40


def normalize_name(name: str) -> str:
    s = (name or "").strip()
    s = s.replace("_", " ")
    return re.sub(r"\s+", " ", s).casefold()


def model_author_keys(model: dict) -> set[str]:
    user = model.get("user") or {}
    keys: set[str] = set()
    for field in ("username", "displayName"):
        v = str(user.get(field) or "").strip()
        if v:
            keys.add(normalize_name(v))
    return keys


def author_matches(model: dict, author: str) -> bool:
    """True if author string matches model owner username or display name."""
    want = normalize_name(author)
    if not want:
        return True
    keys = model_author_keys(model)
    if want in keys:
        return True
    # Allow "Artist" matching displayName "Artist Studio" when it's a whole token.
    for k in keys:
        if want == k or f" {want} " in f" {k} " or k.startswith(want + " ") or k.endswith(" " + want):
            return True
    return False


def _scan_user_models(client: SketchfabClient, username: str, want_title: str) -> dict | None:
    cursor = None
    for _ in range(_MAX_USER_PAGES):
        data = client.list_user_models(username, cursor_url=cursor)
        for m in data.get("results") or []:
            if isinstance(m, dict) and normalize_name(str(m.get("name") or "")) == want_title:
                return m
        cursor = data.get("next")
        if not cursor:
            break
    return None


def resolve_by_title(
    client: SketchfabClient,
    title: str,
    author: str | None = None,
    *,
    try_user_list: bool = True,
) -> dict | None:
    """
    Find a public model dict by exact title, optionally scoped to author.

    Strategy:
    1. If author given and profile exists → paginate GET /models?user= (best match).
    2. Search /search?q=title (+ author in query) → exact title + author filter.

    Sketchfab has no GET /models/by-title endpoint — UID always comes from a lookup.
    Download API still requires that UID: GET /models/{uid}/download.
    """
    title = (title or "").strip()
    if not title:
        return None
    author = (author or "").strip()
    want_title = normalize_name(title)

    if try_user_list and author:
        try:
            user = client.get_user(author)
            uname = str(user.get("username") or author).strip()
            if uname:
                hit = _scan_user_models(client, uname, want_title)
                if hit:
                    return hit
        except LookupError:
            pass
        except Exception:
            pass

    queries = [title]
    if author:
        queries.append(f"{title} {author}")

    best: dict | None = None
    for q in queries:
        try:
            data = client.search_models(query=q, count=24, sort_by="-likeCount")
        except Exception:
            continue
        hits: list[dict] = []
        for m in data.get("results") or []:
            if not isinstance(m, dict):
                continue
            if normalize_name(str(m.get("name") or "")) != want_title:
                continue
            if author and not author_matches(m, author):
                continue
            hits.append(m)
        if len(hits) == 1:
            return hits[0]
        if hits:
            best = hits[0]
    return best


def resolve_uid(client: SketchfabClient, title: str, author: str | None = None) -> str | None:
    m = resolve_by_title(client, title, author)
    if not m:
        return None
    uid = str(m.get("uid") or "").strip()
    return uid or None


def series_prefix_from_name(name: str) -> str | None:
    """
    Infer a shared prefix for family expansion, e.g.
    Series Prefix_Ais_T3 → Series Prefix
    Artist_Character_03 → Artist
    """
    s = (name or "").strip()
    if not s:
        return None
    m = re.match(r"^(.+)[_ ](.+?)[_ ]T\d+$", s, re.I)
    if m:
        return m.group(1).strip()
    m2 = re.match(r"^(.+)[_ ](.+?)[_ ]\d{1,2}$", s)
    if m2:
        return m2.group(1).strip()
    if "_" in s:
        return s.split("_", 1)[0].strip()
    parts = s.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0].strip()
    return None


def middles_for_prefix(names: list[str], prefix: str) -> set[str]:
    """Middle segments sharing a title prefix (Series Prefix_{mid}_T3, etc.)."""
    pref = normalize_name(prefix)
    if not pref:
        return set()
    out: set[str] = set()
    for raw in names:
        n = (raw or "").strip()
        if not n or normalize_name(n) == pref:
            continue
        nn = normalize_name(n)
        if not nn.startswith(pref + " "):
            continue
        rest = nn[len(pref) + 1 :]
        # middle before T# or trailing number
        m = re.match(r"(.+?)(?: t(\d+)| (\d{1,2}))?$", rest, re.I)
        if m:
            mid = (m.group(1) or "").strip()
            if mid:
                out.add(mid.replace(" ", "_"))
    # underscore form: Prefix_Middle_T3
    esc = re.escape(prefix.replace(" ", "_"))
    pat = re.compile(rf"^{esc}_(.+?)(?:_T(\d+)|_(\d{{1,2}}))?$", re.I)
    pat2 = re.compile(rf"^{esc}_(.+)$", re.I)
    for raw in names:
        m = pat.match((raw or "").strip()) or pat2.match((raw or "").strip())
        if m:
            mid = (m.group(1) or "").strip()
            if mid:
                out.add(mid)
    return out
