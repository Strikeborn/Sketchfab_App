"""Global N (NSFW), W (female), and M (male) content filters — mannequins exempt from W/M text matches."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import yaml

_NSFW_RE = re.compile(
    r"\b(nsfw|nude|nudity|naked|topless|bottomless|porn|hentai|erotic|sexual|"
    r"lingerie|bikini|swimsuit|swimwear|swim suit|one[\s-]?piece|two[\s-]?piece|"
    r"thong|underwear|undergarment|panties|panty|bra\b|bralette|areola|nipple|genital|"
    r"xxx|18\+|adult only|stripper|fetish|ecchi|lewd|nsfl|revealing)\b",
    re.I,
)
_FEMALE_RE = re.compile(
    r"\b(woman|women|female|girl|girls|lady|ladies|gal|gals|waifu|"
    r"princess|actress|heroine|schoolgirl|cheerleader|maid|bride|"
    r"mother|mommy|sister|daughter|girlfriend|wife|vixen|pin[\s-]?up|"
    r"character girl|girl character|anime girl|game girl)\b",
    re.I,
)
_MALE_RE = re.compile(
    r"\b(man|men|male|boy|boys|guy|guys|dude|dudes|husband|father|dad|daddy|"
    r"brother|son|boyfriend|schoolboy|anime boy|boy character|"
    r"male character)\b",
    re.I,
)
_MANNEQUIN_RE = re.compile(
    r"\b(mannequin|manikin|dress form|display dummy|store dummy|"
    r"clothing form|tailor dummy|headless mannequin)\b",
    re.I,
)
# Collection-name buckets (Assigned / Already In) — quicker than text heuristics.
_COLL_NSFW_RE = re.compile(r"\b(nsfw|n\s*collection|adult|xxx|18\+|erotica)\b", re.I)
_COLL_FEMALE_RE = re.compile(r"\bfemale\b", re.I)
_COLL_MALE_RE = re.compile(r"\bmale\b", re.I)

_TEXT_COLS = ("Name", "Model Name", "Tags", "Categories", "Author", "License")
_COLLECTION_COLS = ("Assigned Collection(s)", "Already In Collection(s)")

_FEMALE_TOKENS: frozenset[str] | None = None
_MALE_TOKENS: frozenset[str] | None = None
_QUICK_BUCKETS: dict[str, frozenset[str]] | None = None


def _norm_compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _load_token_yaml(path: Path) -> frozenset[str]:
    tokens: set[str] = set()
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for n in data.get("names") or []:
                t = _norm_compact(str(n))
                if t:
                    tokens.add(t)
        except OSError:
            pass
    return frozenset(tokens)


def _load_female_tokens() -> frozenset[str]:
    global _FEMALE_TOKENS
    if _FEMALE_TOKENS is None:
        _FEMALE_TOKENS = _load_token_yaml(Path("data/female_filter_terms.yaml"))
    return _FEMALE_TOKENS


def _load_male_tokens() -> frozenset[str]:
    global _MALE_TOKENS
    if _MALE_TOKENS is None:
        _MALE_TOKENS = _load_token_yaml(Path("data/male_filter_terms.yaml"))
    return _MALE_TOKENS


def _load_quick_buckets() -> dict[str, frozenset[str]]:
    """F/M/N quick-assign targets → filter buckets (casefolded names)."""
    global _QUICK_BUCKETS
    if _QUICK_BUCKETS is not None:
        return _QUICK_BUCKETS
    buckets: dict[str, set[str]] = {"N": set(), "W": set(), "M": set()}
    try:
        from quick_assign import load_quick_assign

        qa = load_quick_assign()
        for letter, bucket in (("N", "N"), ("F", "W"), ("M", "M")):
            name = str(qa.get(letter) or "").strip()
            if name:
                buckets[bucket].add(name.casefold())
    except Exception:
        buckets["N"].add("n collection")
        buckets["W"].add("female")
        buckets["M"].add("male")
    # Always treat common aliases as filter hits.
    buckets["N"].update({"nsfw", "n collection"})
    buckets["W"].add("female")
    buckets["M"].add("male")
    _QUICK_BUCKETS = {k: frozenset(v) for k, v in buckets.items()}
    return _QUICK_BUCKETS


def reload_filter_caches() -> None:
    """Clear cached terms / quick-assign buckets (call after editing YAML)."""
    global _FEMALE_TOKENS, _MALE_TOKENS, _QUICK_BUCKETS
    _FEMALE_TOKENS = None
    _MALE_TOKENS = None
    _QUICK_BUCKETS = None


def _field(row, key: str) -> str:
    if isinstance(row, dict):
        v = row.get(key)
    else:
        if key not in getattr(row, "index", ()):
            return ""
        v = row.get(key)
    if v is None:
        return ""
    return str(v).strip()


def _row_text(row) -> str:
    parts: list[str] = []
    for k in _TEXT_COLS:
        v = _field(row, k)
        if v:
            parts.append(v)
    for k in ("Suggested Collection(s)", "Auto-Assigned Collection(s)", "Fuzzy Match Collection(s)"):
        v = _field(row, k)
        if v:
            parts.append(v)
    return " ".join(parts)


def _row_collections(row) -> list[str]:
    names: list[str] = []
    for col in _COLLECTION_COLS:
        raw = _field(row, col)
        if not raw:
            continue
        for part in raw.split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def _collection_bucket(name: str) -> str | None:
    """Return 'N', 'W', or 'M' if this collection name belongs to a filter bucket."""
    key = name.casefold().strip()
    if not key:
        return None
    buckets = _load_quick_buckets()
    if key in buckets["N"] or _COLL_NSFW_RE.search(name):
        return "N"
    if key in buckets["W"] or _COLL_FEMALE_RE.search(name):
        return "W"
    if key in buckets["M"] or _COLL_MALE_RE.search(name):
        return "M"
    return None


def _row_collection_buckets(row) -> set[str]:
    return {b for name in _row_collections(row) if (b := _collection_bucket(name))}


def _matches_token_list(text: str, tokens: frozenset[str]) -> bool:
    compact = _norm_compact(text)
    if not compact:
        return False
    for tok in tokens:
        if tok in compact:
            return True
    return False


def is_mannequin(row) -> bool:
    return bool(_MANNEQUIN_RE.search(_row_text(row)))


def is_nsfw(row) -> bool:
    if "N" in _row_collection_buckets(row):
        return True
    return bool(_NSFW_RE.search(_row_text(row)))


def is_female_content(row) -> bool:
    # Assignment / membership always counts — mannequin exemption is text-only.
    if "W" in _row_collection_buckets(row):
        return True
    if is_mannequin(row):
        return False
    text = _row_text(row)
    if _FEMALE_RE.search(text):
        return True
    return _matches_token_list(text, _load_female_tokens())


def is_male_content(row) -> bool:
    if "M" in _row_collection_buckets(row):
        return True
    if is_mannequin(row):
        return False
    text = _row_text(row)
    if _MALE_RE.search(text):
        return True
    return _matches_token_list(text, _load_male_tokens())


def collection_hidden_by_filters(
    collection_name: str,
    *,
    hide_nsfw: bool = False,
    hide_female: bool = False,
    hide_male: bool = False,
) -> bool:
    """True if assigning to this collection should hide the row under current N/W/M toggles."""
    bucket = _collection_bucket(collection_name)
    if not bucket:
        return False
    if hide_nsfw and bucket == "N":
        return True
    if hide_female and bucket == "W":
        return True
    if hide_male and bucket == "M":
        return True
    return False


def passes_content_filter(
    row,
    *,
    hide_nsfw: bool = False,
    hide_female: bool = False,
    hide_male: bool = False,
) -> bool:
    if hide_nsfw and is_nsfw(row):
        return False
    if hide_female and is_female_content(row):
        return False
    if hide_male and is_male_content(row):
        return False
    return True


def _any_filter(hide_nsfw: bool, hide_female: bool, hide_male: bool) -> bool:
    return hide_nsfw or hide_female or hide_male


def filter_dataframe(
    df: pd.DataFrame,
    *,
    hide_nsfw: bool = False,
    hide_female: bool = False,
    hide_male: bool = False,
) -> pd.DataFrame:
    if df is None or df.empty or not _any_filter(hide_nsfw, hide_female, hide_male):
        return df
    mask = [
        passes_content_filter(row, hide_nsfw=hide_nsfw, hide_female=hide_female, hide_male=hide_male)
        for _, row in df.iterrows()
    ]
    return df.loc[mask].copy()


def filter_rows(
    rows: list[dict],
    *,
    hide_nsfw: bool = False,
    hide_female: bool = False,
    hide_male: bool = False,
) -> list[dict]:
    if not rows or not _any_filter(hide_nsfw, hide_female, hide_male):
        return rows
    return [
        r
        for r in rows
        if passes_content_filter(r, hide_nsfw=hide_nsfw, hide_female=hide_female, hide_male=hide_male)
    ]


def filter_collections_df(
    df: pd.DataFrame,
    *,
    hide_nsfw: bool = False,
    hide_female: bool = False,
    hide_male: bool = False,
) -> pd.DataFrame:
    """Hide collection rows whose name or model list matches N/W/M filters."""
    if df is None or df.empty or not _any_filter(hide_nsfw, hide_female, hide_male):
        return df
    keep = []
    for _, row in df.iterrows():
        name = row.get("Collection Name")
        probe = {
            "Name": name,
            "Tags": row.get("Model Names"),
            "Categories": name,
            # Treat the collection itself as membership so N/W/M hide Female / N Collection / Male rows.
            "Assigned Collection(s)": name,
        }
        if passes_content_filter(
            probe, hide_nsfw=hide_nsfw, hide_female=hide_female, hide_male=hide_male
        ):
            keep.append(row)
    if not keep:
        return df.iloc[0:0].copy()
    return pd.DataFrame(keep).reset_index(drop=True)
