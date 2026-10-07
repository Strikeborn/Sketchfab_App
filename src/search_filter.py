"""Filter liked models for the Search tab."""
from __future__ import annotations

from collections import Counter

import pandas as pd


def _yes(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin(["yes", "y", "true", "1"])


def _nonempty(series: pd.Series) -> pd.Series:
    ss = series.fillna("").astype(str).str.strip().str.lower()
    return ~ss.isin(["", "none", "nan", "<na>"])


def _split_csv(val: object) -> list[str]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return []
    return [t.strip() for t in str(val).split(",") if t.strip()]


def _csv_name_set(val: object) -> set[str]:
    """Collection/tag names from a CSV cell (strip fuzzy `Name:score` suffixes)."""
    out: set[str] = set()
    for part in _split_csv(val):
        name = part.split(":", 1)[0].strip() if ":" in part else part.strip()
        if name:
            out.add(name.casefold())
    return out


def _cell_has_any_name(cell, wants: list[str]) -> bool:
    parts = _csv_name_set(cell)
    return any(w.casefold() in parts for w in wants if w)


def _cell_has_all_names(cell, wants: list[str]) -> bool:
    parts = _csv_name_set(cell)
    return all(w.casefold() in parts for w in wants if w)


def normalize_license_label(raw: object) -> str:
    """
    Display form for License cell.
    Older Collect wrote labels via .strip('()'), which dropped the closing ')'.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    s = str(raw).strip()
    if not s or s.lower() in {"none", "nan", "<na>"}:
        return ""
    if "(" in s and not s.endswith(")"):
        s = s + ")"
    return s


def license_filter_key(raw: object) -> str:
    """Stable key for copyright filter grouping (prefer slug inside parens)."""
    label = normalize_license_label(raw)
    if not label:
        return ""
    if "(" in label and label.endswith(")"):
        slug = label[label.rfind("(") + 1 : -1].strip().casefold()
        if slug:
            return slug
    return label.casefold()


def list_licenses(df: pd.DataFrame, limit: int = 40) -> list[tuple[str, str]]:
    """
    Unique copyright types as (filter_key, display_label) sorted by frequency.
    """
    if df is None or df.empty or "License" not in df.columns:
        return []
    counts: Counter = Counter()
    labels: dict[str, str] = {}
    for val in df["License"]:
        key = license_filter_key(val)
        if not key:
            continue
        counts[key] += 1
        if key not in labels:
            labels[key] = normalize_license_label(val)
    return [(k, labels[k]) for k, _ in counts.most_common(limit)]


def list_authors(df: pd.DataFrame, limit: int = 200) -> list[str]:
    if df is None or df.empty or "Author" not in df.columns:
        return []
    s = df["Author"].fillna("").astype(str).str.strip()
    s = s[s != ""]
    return s.value_counts().head(limit).index.tolist()


def list_tags(df: pd.DataFrame, limit: int = 80) -> list[str]:
    if df is None or df.empty or "Tags" not in df.columns:
        return []
    c: Counter = Counter()
    for val in df["Tags"]:
        for t in _split_csv(val):
            c[t.lower()] += 1
    return [t for t, _ in c.most_common(limit)]


def list_categories(df: pd.DataFrame, limit: int = 40) -> list[str]:
    if df is None or df.empty or "Categories" not in df.columns:
        return []
    c: Counter = Counter()
    for val in df["Categories"]:
        for t in _split_csv(val):
            c[t] += 1
    return [t for t, _ in c.most_common(limit)]


def list_collection_names(colls_df: pd.DataFrame, limit: int = 120) -> list[str]:
    if colls_df is None or colls_df.empty:
        return []
    col = "Collection Name" if "Collection Name" in colls_df.columns else None
    if not col:
        return []
    s = colls_df[col].fillna("").astype(str).str.strip()
    return sorted(s[s != ""].unique().tolist())[:limit]


def filter_liked_df(
    df: pd.DataFrame,
    query: str = "",
    author: str = "",
    assignment: str = "all",
    downloadable_only: bool = False,
    on_disk_only: bool = False,
    hide_downloaded: bool = False,
    original_only: bool = False,
    collection: str = "",
    tag: str = "",
    category: str = "",
    license: str = "",
    liked_when: str = "",
    liked_month: str = "",
    liked_sort: str = "order_asc",
    tags: list[str] | None = None,
    collections: list[str] | None = None,
    tags_mode: str = "and",
    collections_mode: str = "or",
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if query.strip():
        q = query.strip().lower()
        name_col = "Name" if "Name" in out.columns else ("Model Name" if "Model Name" in out.columns else None)
        masks: list[pd.Series] = []
        for col in (name_col, "Tags", "Author", "UID", "Assigned Collection(s)", "Suggested Collection(s)", "Categories", "License"):
            if col and col in out.columns:
                masks.append(out[col].fillna("").astype(str).str.lower().str.contains(q, regex=False))
        if masks:
            combined = masks[0]
            for m in masks[1:]:
                combined = combined | m
            out = out.loc[combined]

    if author and author not in ("(all)", ""):
        if "Author" in out.columns:
            out = out.loc[out["Author"].fillna("").astype(str) == author]

    if category and category not in ("(all)", ""):
        if "Categories" in out.columns:
            out = out.loc[
                out["Categories"].fillna("").astype(str).str.lower().str.contains(category.lower(), regex=False)
            ]

    tag_list = [t.strip() for t in (tags or []) if t and str(t).strip() and str(t).strip() not in ("(all)", "")]
    single_tag = (tag or "").strip()
    if single_tag and single_tag not in ("(all)", "") and single_tag.casefold() not in {t.casefold() for t in tag_list}:
        tag_list.append(single_tag)
    if tag_list and "Tags" in out.columns:
        mode = (tags_mode or "and").strip().lower()
        if mode == "or":
            out = out.loc[out["Tags"].map(lambda cell: _cell_has_any_name(cell, tag_list))]
        else:
            out = out.loc[out["Tags"].map(lambda cell: _cell_has_all_names(cell, tag_list))]

    lic_key = (license or "").strip()
    if lic_key and lic_key not in ("(all)", "") and "License" in out.columns:
        want = lic_key.casefold()
        mask = out["License"].map(lambda v: license_filter_key(v) == want)
        out = out.loc[mask]

    if assignment != "all":
        a = out.get("Assigned Collection(s)", pd.Series("", index=out.index))
        already = out.get("Already In Collection(s)", pd.Series("", index=out.index))
        if assignment == "unassigned":
            out = out.loc[~(_nonempty(a) | _nonempty(already))]
        elif assignment == "assigned":
            out = out.loc[_nonempty(a)]
        elif assignment == "in_collection":
            out = out.loc[_nonempty(already)]
        elif assignment == "not_in_collection":
            # Already In empty — may still have local Assigned / pending push.
            out = out.loc[~_nonempty(already)]
        elif assignment == "unlisted":
            mask = already.fillna("").astype(str).str.contains(
                r"(?i)(?:^|,\s*)unlisted(?:\s*,|$)", regex=True
            )
            out = out.loc[mask]

    if downloadable_only and "Downloadable" in out.columns:
        out = out.loc[_yes(out["Downloadable"])]

    if on_disk_only and "Downloaded" in out.columns:
        out = out.loc[_yes(out["Downloaded"])]

    if hide_downloaded and "Downloaded" in out.columns:
        out = out.loc[~_yes(out["Downloaded"])]

    if original_only and "Original Source" in out.columns:
        out = out.loc[_yes(out["Original Source"])]

    coll_list = [
        c.strip() for c in (collections or []) if c and str(c).strip() and str(c).strip() not in ("(all)", "")
    ]
    single_coll = (collection or "").strip()
    if single_coll and single_coll not in ("(all)", "") and single_coll.casefold() not in {c.casefold() for c in coll_list}:
        coll_list.append(single_coll)
    if coll_list:
        # Exact collection name only (Female ≠ Female reference). Membership =
        # Assigned (pending) or Already In — not Suggested/Match noise.
        coll_cols = [
            x for x in ("Assigned Collection(s)", "Already In Collection(s)") if x in out.columns
        ]
        if coll_cols:
            mode = (collections_mode or "or").strip().lower()
            wants = [c.casefold() for c in coll_list if c]
            per_col = [out[col].map(_csv_name_set) for col in coll_cols]

            def _has_want(want: str) -> pd.Series:
                hit = pd.Series(False, index=out.index)
                for sets in per_col:
                    hit = hit | sets.map(lambda s, w=want: w in s)
                return hit

            if mode == "and":
                mask = pd.Series(True, index=out.index)
                for w in wants:
                    mask = mask & _has_want(w)
            else:
                mask = pd.Series(False, index=out.index)
                for w in wants:
                    mask = mask | _has_want(w)
            out = out.loc[mask]

    if (liked_when or liked_month) and "Liked At" in out.columns:
        from liked_dates import row_passes_liked_when

        mask = out["Liked At"].apply(lambda v: row_passes_liked_when(v, liked_when, liked_month))
        out = out.loc[mask]

    sort_key = (liked_sort or "order_asc").strip()
    if sort_key == "order_asc" and "Liked Order" in out.columns:
        out = out.sort_values("Liked Order", ascending=True, na_position="last", kind="stable")
    elif sort_key == "order_desc" and "Liked Order" in out.columns:
        out = out.sort_values("Liked Order", ascending=False, na_position="last", kind="stable")
    elif sort_key == "liked_at_desc" and "Liked At" in out.columns:
        out = out.sort_values("Liked At", ascending=False, na_position="last", kind="stable")
    elif sort_key == "liked_at_asc" and "Liked At" in out.columns:
        out = out.sort_values("Liked At", ascending=True, na_position="last", kind="stable")
    elif sort_key in {"likes_desc", "views_desc", "downloads_desc", "faces_desc"}:
        col_map = {
            "likes_desc": "Likes",
            "views_desc": "Views",
            "downloads_desc": "Downloads",
            "faces_desc": "Face Count",
        }
        col = col_map[sort_key]
        if col in out.columns:
            nums = pd.to_numeric(
                out[col].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )
            out = out.assign(_sort_n=nums).sort_values("_sort_n", ascending=False, na_position="last", kind="stable")
            out = out.drop(columns=["_sort_n"])
    elif sort_key == "name":
        name_col = "Name" if "Name" in out.columns else ("Model Name" if "Model Name" in out.columns else None)
        if name_col:
            out = out.sort_values(name_col, ascending=True, na_position="last", kind="stable")

    return out
