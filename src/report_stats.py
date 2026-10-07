"""Report analytics: tag sources and bulk-assignment candidates."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import pandas as pd

from matching import Terms, collect_signals


def _nonempty(s: pd.Series) -> pd.Series:
    ss = s.fillna("").astype(str).str.strip().str.lower()
    return ~ss.isin(["", "none", "nan", "<na>"])


def unassigned_df(liked_df: pd.DataFrame) -> pd.DataFrame:
    if liked_df is None or liked_df.empty:
        return pd.DataFrame()
    a = liked_df.get("Assigned Collection(s)", pd.Series(index=liked_df.index, dtype=object))
    already = liked_df.get("Already In Collection(s)", pd.Series(index=liked_df.index, dtype=object))
    return liked_df.loc[~(_nonempty(a) | _nonempty(already))].copy()


def _split_tags(s: object) -> list[str]:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return []
    return [t.strip().lower() for t in str(s).split(",") if t.strip()]


def _name_tokens(name: str, min_len: int = 4) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(t) >= min_len]


def mine_author_tags(df: pd.DataFrame, top_k: int = 25) -> list[tuple[str, int]]:
    c: Counter = Counter()
    for tags in df.get("Tags", pd.Series(dtype=object)):
        for t in _split_tags(tags):
            c[t] += 1
    return c.most_common(top_k)


def mine_categories(df: pd.DataFrame, top_k: int = 15) -> list[tuple[str, int]]:
    """Sketchfab categories when collected (API field). Not audience tags."""
    if "Categories" not in df.columns:
        return []
    c: Counter = Counter()
    for val in df["Categories"]:
        for t in _split_tags(val):
            c[t] += 1
    return c.most_common(top_k)


def mine_name_tokens(df: pd.DataFrame, top_k: int = 25, min_len: int = 4) -> list[tuple[str, int]]:
    c: Counter = Counter()
    for _, r in df.iterrows():
        name = str(r.get("Name") or r.get("Model Name") or "")
        for t in _name_tokens(name, min_len):
            c[t] += 1
    return c.most_common(top_k)


def mine_suggested_terms(df: pd.DataFrame, top_k: int = 25) -> list[tuple[str, int]]:
    c: Counter = Counter()
    for col in ("Suggested Collection(s)", "Auto-Assigned Collection(s)"):
        if col not in df.columns:
            continue
        for val in df[col]:
            for t in _split_tags(val):
                c[t] += 1
    return c.most_common(top_k)


def mine_fuzzy_hits(df: pd.DataFrame, top_k: int = 20) -> list[tuple[str, int]]:
    c: Counter = Counter()
    col = "Fuzzy Match Collection(s)" if "Fuzzy Match Collection(s)" in df.columns else "Fuzzy Matched Collection(s)"
    if col not in df.columns:
        return []
    for val in df[col]:
        for part in _split_tags(val):
            name = part.split(":")[0].strip() if ":" in part else part
            if name:
                c[name] += 1
    return c.most_common(top_k)


def bulk_assignment_candidates(
    liked_df: pd.DataFrame,
    colls_df: pd.DataFrame,
    terms: Terms | None = None,
    min_models: int = 3,
) -> list[dict]:
    """Per-collection counts of unassigned models matching via different signals."""
    pending = unassigned_df(liked_df)
    if pending.empty or colls_df is None or colls_df.empty:
        return []

    coll_names = [str(n).strip() for n in colls_df.get("Collection Name", pd.Series(dtype=object)) if str(n).strip()]
    name_to_uid: dict[str, str] = {}
    name_to_slug: dict[str, str] = {}
    if "Collection Name" in colls_df.columns:
        for _, crow in colls_df.iterrows():
            n = str(crow.get("Collection Name", "")).strip()
            u = str(crow.get("Collection UID", "")).strip()
            sl = str(crow.get("Slug", "")).strip() if "Slug" in colls_df.columns else ""
            if n:
                name_to_uid[n] = u
                if sl:
                    name_to_slug[n] = sl
    results: list[dict] = []

    for coll in coll_names:
        cuid = name_to_uid.get(coll, "")
        cslug = name_to_slug.get(coll, "")
        cl = coll.lower()
        tag_hit = name_hit = sugg_hit = fuzzy_hit = rule_hit = 0
        matched_idx: set[int] = set()

        for idx, r in pending.iterrows():
            tags = set(_split_tags(r.get("Tags")))
            name = str(r.get("Name") or r.get("Model Name") or "").lower()
            suggested = set(_split_tags(r.get("Suggested Collection(s)")))
            fuzzy_col = r.get("Fuzzy Match Collection(s)") or r.get("Fuzzy Matched Collection(s)") or ""
            fuzzy_names = set()
            for part in _split_tags(fuzzy_col):
                fuzzy_names.add(part.split(":")[0].strip().lower())

            hit = False
            if cl in tags or any(cl in t for t in tags):
                tag_hit += 1
                hit = True
            if cl in name:
                name_hit += 1
                hit = True
            if any(s.lower() == cl for s in suggested):
                sugg_hit += 1
                hit = True
            if cl in fuzzy_names:
                fuzzy_hit += 1
                hit = True

            if terms and not hit:
                desc = str(r.get("Description") or "")
                tag_list = [t.strip() for t in str(r.get("Tags") or "").split(",") if t.strip()]
                sig = collect_signals(name, desc, tag_list, terms)
                if coll in sig.tag_hits or coll in sig.rule_hits or coll in sig.fuzzy_hits:
                    rule_hit += 1
                    hit = True

            if hit:
                matched_idx.add(idx)

        total = len(matched_idx)
        if total < min_models:
            continue

        votes = sum(1 for x in (tag_hit, name_hit, sugg_hit, fuzzy_hit, rule_hit) if x > 0)
        confidence = "high" if votes >= 2 or (tag_hit >= min_models) else ("medium" if votes >= 1 else "low")

        results.append({
            "collection": coll,
            "collection_uid": cuid,
            "collection_slug": cslug,
            "total": total,
            "tag_hit": tag_hit,
            "name_hit": name_hit,
            "suggested": sugg_hit,
            "fuzzy": fuzzy_hit,
            "rules": rule_hit,
            "confidence": confidence,
            "signals": votes,
        })

    results.sort(key=lambda x: (-x["total"], x["collection"]))
    return results[:30]
