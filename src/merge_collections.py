from __future__ import annotations
import logging
import os
from typing import List, Tuple

import pandas as pd
import yaml
from rapidfuzz import fuzz

try:
    from .sketchfab_client import SketchfabClient
except ImportError:
    from sketchfab_client import SketchfabClient


logger = logging.getLogger(__name__)

_DEFAULT_OK_PAIRS: list[tuple[str, str]] = [
    ("Male Reference", "Female reference"),
    ("Chess", "Chests"),
]

_SIMILAR_OK_PATH = os.environ.get(
    "SIMILAR_OK_PATH", os.path.join("terms", "similar_ok.yaml")
)


def _pair_key(a: str, b: str) -> tuple[str, str]:
    x, y = a.strip().lower(), b.strip().lower()
    return (x, y) if x <= y else (y, x)


def load_ok_pairs(path: str | None = None) -> list[tuple[str, str]]:
    p = path or _SIMILAR_OK_PATH
    pairs = list(_DEFAULT_OK_PAIRS)
    if not os.path.isfile(p):
        return pairs
    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for pair in data.get("ok_pairs") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                pairs.append((str(pair[0]), str(pair[1])))
    except Exception as e:
        logger.warning("Could not load similar_ok pairs from %s: %s", p, e)
    return pairs


def is_known_ok_pair(a: str, b: str, ok_pairs: list[tuple[str, str]] | None = None) -> bool:
    key = _pair_key(a, b)
    for x, y in ok_pairs or load_ok_pairs():
        if _pair_key(x, y) == key:
            return True
    return False


def filter_similar_pairs(
    pairs: List[Tuple[int, int, int]],
    cols: pd.DataFrame,
    ok_pairs: list[tuple[str, str]] | None = None,
) -> List[Tuple[int, int, int]]:
    ok = ok_pairs if ok_pairs is not None else load_ok_pairs()
    out: list[tuple[int, int, int]] = []
    for i, j, score in pairs:
        a = str(cols.iloc[i].get("Collection Name", ""))
        b = str(cols.iloc[j].get("Collection Name", ""))
        if is_known_ok_pair(a, b, ok):
            continue
        out.append((i, j, score))
    return out


def find_similar_collections(
    cols: pd.DataFrame,
    threshold: int = 90,
    ok_pairs: list[tuple[str, str]] | None = None,
) -> List[Tuple[int, int, int]]:
    pairs = []
    names = cols["Collection Name"].tolist()
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            s = fuzz.ratio(names[i].lower(), names[j].lower())
            if s >= threshold:
                pairs.append((i, j, s))
    ranked = sorted(pairs, key=lambda x: -x[2])
    return filter_similar_pairs(ranked, cols, ok_pairs=ok_pairs)


def interactive_merge(cols_df: pd.DataFrame, client: SketchfabClient) -> None:
    pairs = find_similar_collections(cols_df)
    if not pairs:
        print("No similar collections found.")
        return

    for i, j, s in pairs:
        a = cols_df.iloc[i]
        b = cols_df.iloc[j]
        print(f"\nSimilar ({s}): \n [A] {a['Collection Name']} ({a['Collection UID']}) \n [B] {b['Collection Name']} ({b['Collection UID']})")
        choice = input("Keep which? [A/B/skip] ").strip().lower()
        if choice not in {"a", "b"}:
            print("Skipping.")
            continue
        keep = a if choice == "a" else b
        drop = b if choice == "a" else a
        print(f"Merging into: {keep['Collection Name']} and deleting {drop['Collection Name']}")

        # Move items from drop -> keep
        drop_items = client.list_models_in_collection(drop["Collection UID"])
        for mu in drop_items:
            try:
                client.add_model_to_collection(keep["Collection UID"], mu)
            except Exception as e:
                logger.error("Failed moving %s: %s", mu, e)
        # Delete the now-empty collection? (Optional; API permissions dependent)
        # If allowed: client._request("DELETE", f"/collections/{drop['Collection UID']}")
        print("Done moving models for this pair. Review API perms before deleting collections.")