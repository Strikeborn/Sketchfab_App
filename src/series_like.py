"""Discover name-series variants (Series Prefix_*_T0–T10, etc.) and like them on Sketchfab."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from model_resolve import (
    author_matches,
    middles_for_prefix,
    normalize_name,
    resolve_by_title,
    series_prefix_from_name,
)
from sketchfab_client import SketchfabClient, humanize_sketchfab_error

_COMPASS_PREFIX = re.compile(r"^Series\s+Compass", re.I)
_COMPASS_MIDDLE = re.compile(
    r"^Series\s+Compass[_ ](.+?)(?:[_ ]T(\d+)|[_ ](\d{1,2}))?\s*$",
    re.I,
)
_T_SUFFIX = re.compile(r"^(.+)_T(\d+)$", re.I)
_NUM_SUFFIX = re.compile(r"^(.+)_(\d{1,2})$")


def parse_compass_middle(name: str) -> str | None:
    m = _COMPASS_MIDDLE.match((name or "").strip())
    if not m:
        return None
    mid = (m.group(1) or "").strip()
    return mid or None


def is_compass_name(name: str) -> bool:
    return bool(_COMPASS_PREFIX.match((name or "").strip()))


def collect_compass_middles(names: list[str]) -> set[str]:
    out: set[str] = set()
    for n in names:
        mid = parse_compass_middle(n)
        if mid:
            out.add(mid)
    return out


def _stem_for_variants(seed: str) -> str:
    """Strip trailing _T# or _## so we can expand T0–T10 and 0–10 on the same base."""
    seed = (seed or "").strip()
    m = _T_SUFFIX.match(seed)
    if m:
        return m.group(1)
    m2 = _NUM_SUFFIX.match(seed)
    if m2 and not _T_SUFFIX.search(seed):
        return m2.group(1)
    return seed


def _register_title_aliases(out: set[str], s: str) -> None:
    """Underscore, space-separated, and glued (no _) spellings."""
    s = (s or "").strip()
    if not s:
        return
    out.add(s)
    if "_" in s:
        out.add(s.replace("_", " "))
    # Glued suffix: Series Prefix_Ais_T3 → Series Prefix_AisT3 / Series Prefix AisT3
    m = re.match(r"^(.+)[_ ](T\d+|\d{1,2})$", s, re.I)
    if m:
        stem, suffix = m.group(1), m.group(2)
        out.add(f"{stem}{suffix}")
        if "_" in stem:
            out.add(f"{stem.replace('_', ' ')}{suffix}")


def generate_variants(seed: str, *, t_max: int = 10) -> set[str]:
    """Build T0–T10, plain 0–10, zero-padded, space, and no-underscore variants."""
    seed = (seed or "").strip()
    if not seed:
        return set()

    out: set[str] = set()

    def add(s: str) -> None:
        _register_title_aliases(out, s)

    stem = _stem_for_variants(seed)
    add(seed)
    add(stem)
    for i in range(t_max + 1):
        add(f"{stem}_T{i}")
        add(f"{stem}T{i}")
        add(f"{stem} T{i}")
    for i in range(t_max + 1):
        add(f"{stem}_{i}")
        add(f"{stem}{i}")
        add(f"{stem}_{i:02d}")
        add(f"{stem} {i}")

    return out


def compass_family_candidates(middles: set[str], *, t_max: int = 10) -> set[str]:
    out: set[str] = set()
    for mid in sorted(middles, key=str.casefold):
        mid = mid.strip()
        if not mid:
            continue
        for i in range(t_max + 1):
            for s in (
                f"Series Prefix_{mid}_T{i}",
                f"Series Prefix {mid} T{i}",
                f"Series Prefix_{mid}T{i}",
                f"Series Prefix {mid}T{i}",
            ):
                _register_title_aliases(out, s)
        for s in (f"Series Prefix_{mid}", f"Series Prefix {mid}"):
            _register_title_aliases(out, s)
        for i in range(t_max + 1):
            for s in (
                f"Series Prefix_{mid}_{i:02d}",
                f"Series Prefix_{mid}_{i}",
                f"Series Prefix {mid} {i:02d}",
                f"Series Prefix {mid} {i}",
                f"Series Prefix_{mid}{i}",
                f"Series Prefix {mid}{i}",
            ):
                _register_title_aliases(out, s)
    return out


def family_candidates_for_prefix(prefix: str, middles: set[str], *, t_max: int = 10) -> set[str]:
    """Generic T0–T10 / numeric variants for any author prefix (not only Series Prefix)."""
    prefix = (prefix or "").strip()
    if not prefix or not middles:
        return set()
    out: set[str] = set()
    sep_u = "_" if "_" in prefix or any("_" in m for m in middles) else " "
    for mid in sorted(middles, key=str.casefold):
        mid = mid.strip()
        if not mid:
            continue
        base_u = f"{prefix}_{mid}" if sep_u == "_" else f"{prefix} {mid}"
        for i in range(t_max + 1):
            for s in (
                (f"{base_u}_T{i}", f"{base_u} T{i}", f"{base_u}T{i}")
                if sep_u == "_"
                else (f"{base_u} T{i}", f"{base_u}T{i}")
            ):
                _register_title_aliases(out, s)
        _register_title_aliases(out, base_u)
        for i in range(t_max + 1):
            for s in (
                (f"{base_u}_{i:02d}", f"{base_u}_{i}", f"{base_u} {i}", f"{base_u}{i}")
                if sep_u == "_"
                else (f"{base_u} {i:02d}", f"{base_u} {i}", f"{base_u}{i}")
            ):
                _register_title_aliases(out, s)
    return out


def wanted_variant_norms(
    seed_names: list[str],
    all_liked_names: list[str],
    *,
    t_max: int = 10,
    expand_compass_family: bool = True,
    expand_generic_family: bool = True,
) -> set[str]:
    """Normalized title patterns to match from broad search results."""
    names = build_candidate_names(
        seed_names,
        all_liked_names,
        t_max=t_max,
        expand_compass_family=expand_compass_family,
        expand_generic_family=expand_generic_family,
    )
    return {normalize_name(n) for n in names if (n or "").strip()}


def broad_variant_search_queries(
    seed_names: list[str],
    all_liked_names: list[str],
    author: str,
    *,
    max_middles: int = 35,
    max_prefixes: int = 12,
) -> list[str]:
    """Few broad catalog queries instead of one API call per candidate title."""
    author = (author or "").strip()
    names = list(dict.fromkeys([*(seed_names or []), *(all_liked_names or [])]))
    middles = sorted(collect_compass_middles(names), key=str.casefold)[:max(1, max_middles)]
    raw: list[str] = []

    if author:
        raw.extend([f"Series Prefix {author}", f"{author} Series Prefix", f"{author} Series"])
    raw.append("Series Prefix")

    for mid in middles:
        raw.append(f"Series Prefix {mid}")
        if author:
            raw.append(f"Series Prefix {mid} {author}")

    prefixes: set[str] = set()
    for n in names:
        p = series_prefix_from_name(n)
        if p and not is_compass_name(p):
            prefixes.add(p)
    for p in sorted(prefixes, key=str.casefold)[:max(1, max_prefixes)]:
        if author:
            raw.append(f"{p} {author}")
        raw.append(p)

    seen: set[str] = set()
    out: list[str] = []
    for q in raw:
        q = q.strip()
        key = q.casefold()
        if q and key not in seen:
            seen.add(key)
            out.append(q)
    return out


def collect_variant_hits_broad(
    client: SketchfabClient,
    queries: list[str],
    keys: set[str],
    wanted_norms: set[str],
    *,
    existing_uids: set[str],
    existing_norms: set[str],
    author: str = "",
    max_pages_per_query: int = 3,
    on_progress=None,
    should_cancel=None,
    pause_s: float = 0.12,
) -> tuple[list[dict], int]:
    """
    Paginate a small set of broad searches; match author + wanted title patterns locally.
    Returns (raw model dicts, api_pages_fetched).
    """
    hits: list[dict] = []
    seen_uids: set[str] = set()
    pages = 0
    total_q = len(queries)

    for qi, query in enumerate(queries, start=1):
        if should_cancel and should_cancel():
            break
        cursor = None
        for _ in range(max(1, max_pages_per_query)):
            if should_cancel and should_cancel():
                break
            if on_progress:
                try:
                    on_progress(qi, total_q, f"Broad search {qi}/{total_q}: {query[:44]}")
                except Exception:
                    pass
            try:
                data = client.search_models(query=query, count=24, cursor_url=cursor)
            except Exception:
                break
            pages += 1
            for m in data.get("results") or []:
                if not isinstance(m, dict):
                    continue
                uid = str(m.get("uid") or "").strip()
                if not uid or uid in seen_uids or uid in existing_uids:
                    continue
                if not _model_matches(m, keys):
                    continue
                name = str(m.get("name") or "").strip()
                nn = normalize_name(name)
                if not nn or nn in existing_norms:
                    continue
                if nn not in wanted_norms:
                    continue
                seen_uids.add(uid)
                hits.append(m)
            cursor = data.get("next")
            if not cursor:
                break
            if pause_s > 0:
                time.sleep(pause_s)

    return hits, pages


def _model_matches(model: dict, keys: set[str]) -> bool:
    for k in keys:
        if author_matches(model, k):
            return True
    user = model.get("user") or {}
    blob = " ".join(str(user.get(x) or "") for x in ("username", "displayName", "profileUrl"))
    nb = normalize_name(blob)
    for k in keys:
        if k and k in nb:
            return True
    return False


def build_candidate_names(
    seed_names: list[str],
    all_liked_names: list[str],
    *,
    t_max: int = 10,
    expand_compass_family: bool = True,
    expand_generic_family: bool = True,
) -> list[str]:
    """Deduped search strings, excluding names already in likes (by normalized name)."""
    liked_norm = {normalize_name(n) for n in all_liked_names if (n or "").strip()}
    raw: set[str] = set()

    seeds = [s.strip() for s in seed_names if (s or "").strip()]
    for name in seeds:
        raw.update(generate_variants(name, t_max=t_max))

    if expand_compass_family:
        family_names = list(all_liked_names) + seeds
        middles = collect_compass_middles(family_names)
        if not middles and seeds:
            middles = collect_compass_middles(seeds)
        if middles:
            raw.update(compass_family_candidates(middles, t_max=t_max))

    if expand_generic_family:
        prefixes: set[str] = set()
        for name in seeds:
            p = series_prefix_from_name(name)
            if p:
                prefixes.add(p)
        family_names = list(all_liked_names) + seeds
        for name in family_names:
            p = series_prefix_from_name(name)
            if p:
                prefixes.add(p)
        for prefix in prefixes:
            if is_compass_name(prefix):
                continue
            mids = middles_for_prefix(family_names, prefix)
            if mids:
                raw.update(family_candidates_for_prefix(prefix, mids, t_max=t_max))

    ordered: list[str] = []
    seen_norm: set[str] = set()
    for cand in sorted(raw, key=str.casefold):
        n = normalize_name(cand)
        if not n or n in liked_norm or n in seen_norm:
            continue
        seen_norm.add(n)
        ordered.append(cand)
    return ordered


def search_exact_name(
    client: SketchfabClient,
    candidate: str,
    author: str | None = None,
) -> dict | None:
    """Exact title match; optional author scopes deleted-account recovery."""
    if author and (author or "").strip():
        return resolve_by_title(client, candidate, author)
    want = normalize_name(candidate)
    if not want:
        return None
    try:
        data = client.search_models(query=candidate, count=24, sort_by="-likeCount")
    except Exception:
        return None
    for m in data.get("results") or []:
        if not isinstance(m, dict):
            continue
        if normalize_name(str(m.get("name") or "")) == want:
            return m
    return None


@dataclass
class SeriesLikeResult:
    candidates: int = 0
    found: int = 0
    liked: int = 0
    already_liked: int = 0
    not_found: int = 0
    errors: list[str] = field(default_factory=list)
    newly_liked_uids: list[str] = field(default_factory=list)
    found_names: list[str] = field(default_factory=list)


def run_series_like(
    client: SketchfabClient,
    *,
    seed_names: list[str],
    all_liked_names: list[str],
    liked_uids: set[str],
    seed_authors: list[str] | None = None,
    t_max: int = 10,
    expand_compass_family: bool = True,
    on_progress=None,
    should_cancel=None,
    pause_s: float = 0.05,
) -> SeriesLikeResult:
    candidates = build_candidate_names(
        seed_names,
        all_liked_names,
        t_max=t_max,
        expand_compass_family=expand_compass_family,
    )
    res = SeriesLikeResult(candidates=len(candidates))
    total = len(candidates)

    author_hint = ""
    if seed_authors:
        for a in seed_authors:
            if (a or "").strip():
                author_hint = a.strip()
                break

    for i, cand in enumerate(candidates, start=1):
        if should_cancel and should_cancel():
            break
        if on_progress:
            try:
                on_progress(i, total, f"Series search {i}/{total}: {cand[:48]}")
            except Exception:
                pass

        model = search_exact_name(client, cand, author_hint or None)
        if not model:
            res.not_found += 1
            if pause_s > 0:
                time.sleep(pause_s)
            continue

        res.found += 1
        uid = str(model.get("uid") or "").strip()
        name = str(model.get("name") or cand).strip()
        if not uid:
            res.not_found += 1
            continue

        if uid.casefold() in {u.casefold() for u in liked_uids}:
            res.already_liked += 1
            if pause_s > 0:
                time.sleep(pause_s)
            continue

        try:
            client.like_model(uid)
            res.liked += 1
            res.newly_liked_uids.append(uid)
            res.found_names.append(name)
            liked_uids.add(uid.casefold())
        except Exception as exc:
            msg = humanize_sketchfab_error(exc)
            low = msg.casefold()
            if "400" in msg and "like" in low:
                res.already_liked += 1
                liked_uids.add(uid.casefold())
            else:
                res.errors.append(f"{name}: {msg}")
        if pause_s > 0:
            time.sleep(pause_s)

    return res
