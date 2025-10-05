from __future__ import annotations
import logging
from typing import Dict

import pandas as pd
from tqdm import tqdm

try:
    from .sketchfab_client import SketchfabClient
except ImportError:
    from sketchfab_client import SketchfabClient


logger = logging.getLogger(__name__)

ASSIGNED_COL = "Assigned Collection(s)"


def push(liked_df: pd.DataFrame, collections_df: pd.DataFrame, client: SketchfabClient, dry_run: bool = False) -> None:
    # Map collection name -> uid
    name_to_uid: Dict[str, str] = {r["Collection Name"]: r["Collection UID"] for _, r in collections_df.iterrows()}

    # Preload models already in collections (to avoid duplicate POSTs)
    existing: Dict[str, set[str]] = {name: set() for name in name_to_uid}
    for name, uid in name_to_uid.items():
        try:
            uids = client.list_models_in_collection(uid)
            existing[name] = set(uids)
        except Exception as e:
            logger.warning("Could not list models for collection %s: %s", name, e)

    ops = []
    for _, row in liked_df.iterrows():
        model_uid = row.get("Model UID")
        want = [s.strip() for s in str(row.get(ASSIGNED_COL) or "").split(",") if s.strip()]
        for coll_name in want:
            if coll_name not in name_to_uid:
                logger.warning("Assigned collection '%s' not found in current collections; skipping model %s", coll_name, model_uid)
                continue
            if model_uid in existing.get(coll_name, set()):
                continue
            ops.append((name_to_uid[coll_name], model_uid, coll_name))

    if not ops:
        logger.info("No assignments to push.")
        return

    logger.info("Pushing %d assignments (dry_run=%s)", len(ops), dry_run)
    for coll_uid, model_uid, coll_name in tqdm(ops, desc="Posting"):
        if dry_run:
            continue
        try:
            client.add_model_to_collection(coll_uid, model_uid)
        except Exception as e:
            logger.error("Failed to add model %s to %s: %s", model_uid, coll_name, e)