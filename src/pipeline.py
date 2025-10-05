from __future__ import annotations
import argparse
import logging
import os

import pandas as pd

try:
    from .collector import build_workbook
    from .sketchfab_client import SketchfabClient
    from .data_io import write_workbook, read_workbook, XL_PATH
    from .matching import Terms
    from .auto_assign import run_auto_assign
    from .push_assignments import push
    from .merge_collections import interactive_merge
except ImportError:
    from collector import build_workbook
    from sketchfab_client import SketchfabClient
    from data_io import write_workbook, read_workbook, XL_PATH
    from matching import Terms
    from auto_assign import run_auto_assign
    from push_assignments import push
    from merge_collections import interactive_merge


LOG_DIR = os.environ.get("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "pipeline.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("pipeline")

TERMS_PATH = os.environ.get("TERMS_PATH", os.path.join("terms", "collections_terms.yaml"))

def cmd_collect(args):
    from .collector import build_workbook
    path = build_workbook()
    logger.info("Wrote workbook: %s", path)
'''
def cmd_collect(args):
    client = SketchfabClient()

    models = client.get_liked_models(progress=args.progress)
    cols = client.get_collections(progress=args.progress)

    liked_df = pd.DataFrame([
        {
            "Model UID": m.uid,
            "Model Name": m.name,
            "Author": m.author,
            "Description": m.description or "",
            "Tags": m.tags,
            "Is Downloadable": bool(m.is_downloadable) if m.is_downloadable is not None else None,
        }
        for m in models
    ])

    col_df = pd.DataFrame([
        {"Collection UID": c.uid, "Collection Name": c.name, "Slug": c.slug}
        for c in cols
    ])

    write_workbook(liked_df, col_df)
    logger.info("Wrote workbook: %s (Liked: %d, Collections: %d)", XL_PATH, len(liked_df), len(col_df))
'''

def cmd_match(args):
    terms = Terms.from_yaml(TERMS_PATH)
    liked_df, cols_df = read_workbook()

    from .auto_assign import SUG_COL, FUZZY_COL
    # Only recompute suggestions; don't touch Assigned here
    updated = run_auto_assign(liked_df, terms, overwrite=False)
    # Preserve the user's existing Assigned/Notes by copying back from liked_df where present
    for col in ["Assigned Collection(s)", "Assignment Notes"]:
        if col in liked_df.columns:
            updated[col] = liked_df[col]

    from .data_io import write_workbook
    write_workbook(updated, cols_df)
    logger.info("Updated suggestions/fuzzy in workbook.")


def cmd_auto_assign(args):
    terms = Terms.from_yaml(TERMS_PATH)
    liked_df, cols_df = read_workbook()

    updated = run_auto_assign(liked_df, terms, overwrite=args.overwrite)
    write_workbook(updated, cols_df)
    logger.info("Wrote auto-assign results (overwrite=%s)", args.overwrite)


def cmd_push(args):
    liked_df, cols_df = read_workbook()
    client = SketchfabClient()
    push(liked_df, cols_df, client, dry_run=args.dry_run)


def cmd_report(args):
    import collections
    liked_df, _ = read_workbook()
    pending = liked_df[(liked_df["Assigned Collection(s)"].isna()) | (liked_df["Assigned Collection(s)"] == "")]
    print(f"Unassigned models: {len(pending)}")

    # Top tokens in names/descriptions to consider adding to terms
    def toks(s: str) -> list[str]:
        import re
        s = (s or "").lower()
        s = re.sub(r"[^a-z0-9\s]+", " ", s)
        return [t for t in s.split() if len(t) >= 4]

    counter = collections.Counter()
    for _, r in pending.iterrows():
        counter.update(toks((r.get("Model Name") or "") + " " + (r.get("Description") or "")))
        counter.update([t.strip().lower() for t in str(r.get("Tags") or "").split(",") if t and len(t.strip()) >= 3])

    print("\nCandidate new terms (top 50):")
    for term, cnt in counter.most_common(50):
        print(f"  {term:20s}  {cnt}")


def cmd_merge(args):
    liked_df, cols_df = read_workbook()
    client = SketchfabClient()
    interactive_merge(cols_df, client)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sketchfab-collections-pipeline", description="Collections auto-assignment pipeline (v5.0)")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp_collect = sp.add_parser("collect", help="Fetch likes + collections to workbook")
    sp_collect.add_argument("--progress", action="store_true", help="Show per-item progress bars and per-page logs")
    sp_collect.set_defaults(func=cmd_collect)

    sp_match = sp.add_parser("match", help="Compute suggestions/fuzzy (preserve Assigned)")
    sp_match.set_defaults(func=cmd_match)

    sp_auto = sp.add_parser("auto-assign", help="Write Assigned per policy")
    sp_auto.add_argument("--overwrite", action="store_true", help="Overwrite any manual Assigned values")
    sp_auto.set_defaults(func=cmd_auto_assign)

    sp_push = sp.add_parser("push", help="POST Assigned -> API")
    sp_push.add_argument("--dry-run", action="store_true", help="Do not call API; just print plan")
    sp_push.set_defaults(func=cmd_push)

    sp_report = sp.add_parser("report", help="Show unassigned counts + candidate terms to refine YAML")
    sp_report.set_defaults(func=cmd_report)

    sp_merge = sp.add_parser("merge-collections", help="Interactive tool to merge duplicate/similar collections")
    sp_merge.set_defaults(func=cmd_merge)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()