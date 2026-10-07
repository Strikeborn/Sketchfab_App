"""Match typed/chip collection names to existing Sketchfab collection titles."""
from __future__ import annotations

import re


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def name_variants(name: str) -> set[str]:
    """Casefold forms including simple English singular/plural variants."""
    base = _norm(name)
    if not base:
        return set()
    out = {base}
    # enemies → enemy, berries → berry
    if base.endswith("ies") and len(base) > 3:
        out.add(base[:-3] + "y")
    # boxes → box, dishes → dish (light touch)
    if base.endswith("ses") and len(base) > 3:
        out.add(base[:-2])
    if base.endswith("es") and len(base) > 2 and not base.endswith("ies"):
        out.add(base[:-2])
        out.add(base[:-1])
    if base.endswith("s") and not base.endswith("ss") and len(base) > 1:
        out.add(base[:-1])
    # singular → plural
    for stem in list(out):
        if stem.endswith("y") and len(stem) > 1 and stem[-2] not in "aeiou":
            out.add(stem[:-1] + "ies")
        elif not stem.endswith("s"):
            out.add(stem + "s")
            out.add(stem + "es")
    return out


def match_collection_name(want: str, names: list[str]) -> str | None:
    """
    Best existing collection for `want`, or None.
    Exact (casefold) → singular/plural twin → unique prefix → unique contains.
    """
    want = (want or "").strip()
    if not want:
        return None
    cleaned = [str(n).strip() for n in names if str(n).strip()]
    if not cleaned:
        return None
    lower_map = {_norm(n): n for n in cleaned}
    key = _norm(want)
    if key in lower_map:
        return lower_map[key]

    variants = name_variants(want)
    hits = [n for n in cleaned if _norm(n) in variants or bool(name_variants(n) & variants)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # Prefer shortest title (Enemy over Enemy Pack if both somehow match)
        hits.sort(key=lambda n: (len(n), _norm(n)))
        # Prefer exact variant equality over looser
        for n in hits:
            if _norm(n) in variants:
                return n
        return hits[0]

    starts = [n for n in cleaned if _norm(n).startswith(key)]
    if len(starts) == 1:
        return starts[0]
    if len(starts) > 1:
        starts.sort(key=lambda n: (len(n), _norm(n)))
        return starts[0]

    contains = [n for n in cleaned if key in _norm(n)]
    if len(contains) == 1:
        return contains[0]
    return None
