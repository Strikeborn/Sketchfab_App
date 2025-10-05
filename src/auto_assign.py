from __future__ import annotations
import logging
import pandas as pd
# replace the top import with this try/fallback
try:
    from .matching import Terms, collect_signals, policy_assign
except ImportError:
    from matching import Terms, collect_signals, policy_assign


logger = logging.getLogger(__name__)

SUG_COL = "Suggested Collection(s)"
FUZZY_COL = "Fuzzy Match Collection(s)"
ASSIGNED_COL = "Assigned Collection(s)"
NOTES_COL = "Assignment Notes"


def run_auto_assign(liked_df: pd.DataFrame, terms: Terms, overwrite: bool = False) -> pd.DataFrame:
    out = liked_df.copy()

    def to_list(x):
        if pd.isna(x) or x is None:
            return []
        if isinstance(x, list):
            return x
        return [s.strip() for s in str(x).replace(";", ",").split(",") if s.strip()]

    sug_list = []
    fuzzy_list = []
    assigned_list = []
    notes_list = []

    for _, row in out.iterrows():
        name = row.get("Model Name") or ""
        desc = row.get("Description") or ""
        tags = [t.strip() for t in str(row.get("Tags") or "").split(",") if t.strip()]

        signals = collect_signals(name, desc, tags, terms)

        # Persist diagnostics
        sug = sorted(signals.tag_hits | signals.rule_hits)
        fuzzy = [f"{k}:{v}" for k, v in sorted(signals.fuzzy_hits.items(), key=lambda kv: kv[0])]

        sug_list.append(", ".join(sug))
        fuzzy_list.append(", ".join(fuzzy))

        # Respect manual assignment unless overwrite
        existing_assigned = to_list(row.get(ASSIGNED_COL))
        if existing_assigned and not overwrite:
            assigned_list.append(", ".join(existing_assigned))
            notes_list.append(row.get(NOTES_COL) or "")
            continue

        pr = policy_assign(signals, terms)
        assigned_list.append(", ".join(pr.assigned))
        notes_list.append(pr.notes)

    out[SUG_COL] = sug_list
    out[FUZZY_COL] = fuzzy_list
    out[ASSIGNED_COL] = assigned_list
    out[NOTES_COL] = notes_list
    return out