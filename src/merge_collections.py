from __future__ import annotations
import logging
from typing import List, Tuple

import pandas as pd
from rapidfuzz import fuzz

try:
    from .sketchfab_client import SketchfabClient
except ImportError:
    from sketchfab_client import SketchfabClient


logger = logging.getLogger(__name__)


def find_similar_collections(cols: pd.DataFrame, threshold: int = 90) -> List[Tuple[int, int, int]]:
    pairs = []
    names = cols["Collection Name"].tolist()
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            s = fuzz.ratio(names[i].lower(), names[j].lower())
            if s >= threshold:
                pairs.append((i, j, s))
    return sorted(pairs, key=lambda x: -x[2])


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