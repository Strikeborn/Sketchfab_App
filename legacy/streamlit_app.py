from __future__ import annotations
import os, sys
sys.path.append(os.path.dirname(__file__))  # so 'from collector import ...' works
import streamlit as st
import pandas as pd
from collector import build_workbook
from data_io import read_workbook, write_workbook, XL_PATH
from matching import Terms
from auto_assign import run_auto_assign
from push_assignments import push
from sketchfab_client import SketchfabClient
from merge_collections import find_similar_collections
import logging
import collections, re
from rapidfuzz import fuzz, process

TERMS_PATH = os.environ.get("TERMS_PATH", os.path.join("terms", "collections_terms.yaml"))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

st.set_page_config(page_title="Sketchfab Collections", layout="wide")
st.title("Sketchfab Collections – v5 UI")
# defaults for persistent toggles
for k, v in {"overwrite": False, "dry_run": True, "report_open": False}.items():
    st.session_state.setdefault(k, v)

st.markdown("""
<style>
.stick { position: sticky; top: 0; z-index: 100;
         background: var(--background-color);
         border-bottom: 1px solid rgba(151,151,151,.2);
         padding: 8px 10px; }
.stick .stButton>button { height:2.2rem; padding:.25rem .75rem }
</style>
""", unsafe_allow_html=True)

def show_status():
    st.caption(f"Workbook path: `{XL_PATH}`")
    tok = os.environ.get("SKETCHFAB_TOKEN")
    st.caption("Token: ✅ set" if tok else "Token: ❌ missing (.env SKETCHFAB_TOKEN)")

show_status()

def toolbar():
    st.markdown('<div class="stick">', unsafe_allow_html=True)
    c1,c2,c3,c4,c5,c6 = st.columns([1,1,1,1,1,2])
    with c1: do_collect = st.button("📥 Collect", use_container_width=True)
    with c2: do_match   = st.button("🧠 Match",  use_container_width=True)
    with c3: st.toggle("Overwrite Assigned", key="overwrite")
    with c4: do_auto    = st.button("🤖 Auto-Assign", use_container_width=True)
    with c5: st.toggle("Dry-run Push", key="dry_run")
    with c6: do_push    = st.button("🚀 Push Assignments", use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)
    return (do_collect, do_match, st.session_state.get("overwrite", False),
            do_auto, st.session_state.get("dry_run", True), do_push)

# --- bottom toolbar (inline; scrolls with page) ---
st.caption("Controls")
do_collect, do_match, overwrite, do_auto, dry_run, do_push = toolbar()
# Load workbook if present
if os.path.exists(XL_PATH):
    liked_df, cols_df = read_workbook()
else:
    liked_df = pd.DataFrame()
    cols_df = pd.DataFrame()

# Preview tabs
tab1, tab2, tab3 = st.tabs(["Liked Models", "Collections", "Report / Terms"])

# --- Liked Models tab ---
with tab1:
    if not liked_df.empty:
        st.caption(f"{len(liked_df):,} liked models")
        show_all_l = st.toggle("Show all liked rows", value=False)
        if show_all_l:
            st.dataframe(liked_df, use_container_width=True, height=600)
        else:
            n = st.slider("Preview liked rows", 100, min(len(liked_df), 10000), 300, 100, key="liked_n")
            st.dataframe(liked_df.head(n), use_container_width=True, height=600)
    else:
        st.info("Run Collect to build the workbook.")

# --- Collections tab ---
with tab2:
    if not cols_df.empty:
        st.caption(f"{len(cols_df):,} collections")
        show_all_c = st.toggle("Show all collections", value=False)
        if show_all_c:
            st.dataframe(cols_df, use_container_width=True, height=600)
        else:
            n2 = st.slider("Preview collections", 50, min(len(cols_df), 10000), 300, 50, key="cols_n")
            st.dataframe(cols_df.head(n2), use_container_width=True, height=600)
    else:
        st.info("No collections yet. Click Collect.")
# -------- Report --------
with tab3:
    import pandas as pd, collections, re
    # Build a single Series of assigned values regardless of column name
    a = liked_df["Assigned Collection"] if "Assigned Collection" in liked_df.columns else pd.Series("", index=liked_df.index)
    b = liked_df["Assigned Collection(s)"] if "Assigned Collection(s)" in liked_df.columns else pd.Series("", index=liked_df.index)
    assigned = a.fillna("").astype(str) + b.fillna("").astype(str)

    pending = liked_df[assigned.str.len() == 0]
    st.write(f"Unassigned models: **{len(pending):,}**")

    # ---- term mining (Name + Tags), interactive and persistent
    def mine_terms(df, top_k=100, min_len=4, sample_n=None):
        counter = collections.Counter()
        if sample_n:
            df = df.head(sample_n)
        for _, r in df.iterrows():
            name = (r.get("Name") or r.get("Model Name") or "")
            tags = str(r.get("Tags") or "")
            tokens = re.findall(r"[a-z0-9]+", (name + " " + tags).lower())
            counter.update([t for t in tokens if len(t) >= min_len])
        return pd.DataFrame(counter.most_common(top_k), columns=["term", "count"])

    st.subheader("Term mining")
    top_k   = st.slider("How many terms", 20, 300, 100, 10, key="tm_topk")
    min_len = st.slider("Min token length", 3, 8, 4, key="tm_minlen")
    sample  = st.slider("Sample first N rows (speed)", 200, min(5000, len(pending) or 200), 2000, 200, key="tm_sample")

    terms_df = mine_terms(pending, top_k=top_k, min_len=min_len, sample_n=sample)
    st.dataframe(terms_df, use_container_width=True)

    # Optional: fuzzy suggest a collection for each term
    from rapidfuzz import process, fuzz
    if not cols_df.empty and st.checkbox("Suggest target collection (fuzzy)", value=False, key="tm_fuzzy"):
        col_names = cols_df["Collection Name"].astype(str).tolist()
        def guess(term):
            hit = process.extractOne(term, col_names, scorer=fuzz.token_sort_ratio)
            return hit[0] if hit and hit[1] >= 80 else ""
        terms_df["suggested_collection"] = terms_df["term"].apply(guess)
        st.dataframe(terms_df, use_container_width=True)

    picked = st.multiselect("Pick terms to add", terms_df["term"].tolist(), key="tm_picked")
    target = st.text_input("Target collection name", key="tm_target")
    if st.button("Build YAML snippet", key="tm_yaml") and target and picked:
        snippet = f"""{target}:
include_terms: [{", ".join(picked)}]
# tag_terms: [{", ".join(picked)}]
exclude_terms: []
fuzzy_threshold: 88"""
        st.code(snippet, language="yaml")

# -------- Merge Duplicates (preview + one-click merge) --------
# Expose the button inline so it remains accessible (was originally in the sidebar)
do_find_dupes = st.button("Find Similar Collections")
if do_find_dupes:
    if cols_df.empty:
        st.error("No collections loaded. Click Collect first.")
    else:
        pairs = find_similar_collections(cols_df, threshold=90)
        if not pairs:
            st.info("No similar collections found.")
        else:
            import numpy as np
            data = []
            for i, j, s in pairs[:100]:
                a = cols_df.iloc[i]
                b = cols_df.iloc[j]
                data.append([a["Collection Name"], b["Collection Name"], int(s), a["Collection UID"], b["Collection UID"]])
            df = pd.DataFrame(data, columns=["Keep A", "Merge B into A", "Similarity", "A_UID", "B_UID"])
            st.dataframe(df.drop(columns=["A_UID","B_UID"]), use_container_width=True)
            st.caption("Pick a row and click Merge to move models from B into A (collections are not deleted).")
            idx = st.number_input("Row index to merge", min_value=0, max_value=len(df)-1, value=0, step=1)
            if st.button("Merge selected"):
                row = df.iloc[int(idx)]
                client = SketchfabClient()
                b_items = client.list_models_in_collection(row["B_UID"])
                moved = 0
                for mu in b_items:
                    try:
                        client.add_model_to_collection(row["A_UID"], mu)
                        moved += 1
                    except Exception:
                        pass
                st.success(f"Merged {moved} models from **{row['Merge B into A']}** → **{row['Keep A']}**.")
                st.info("You can delete the old collection manually in Sketchfab if desired.")

# handlers
if do_collect:
    with st.spinner("Building workbook..."):
        path = build_workbook()
    st.toast(f"Workbook built: {path}", icon="✅")
    st.cache_data.clear()
    st.rerun()

if do_match:
    if liked_df.empty:
        st.error("No workbook yet. Click Collect first.")
    else:
        terms = Terms.from_yaml(TERMS_PATH)
        updated = run_auto_assign(liked_df, terms, overwrite=False)
        for col in ["Assigned Collection(s)", "Assigned Collection", "Assignment Notes"]:
            if col in liked_df.columns and col in updated.columns:
                updated[col] = liked_df[col]
        write_workbook(updated, cols_df)
        st.success("Suggestions/Fuzzy updated.")

if do_auto:
    if liked_df.empty:
        st.error("No workbook yet. Click Collect first.")
    else:
        terms = Terms.from_yaml(TERMS_PATH)
        updated = run_auto_assign(liked_df, terms, overwrite=overwrite)
        write_workbook(updated, cols_df)
        st.success(f"Auto-assign complete (overwrite={overwrite}).")

if do_push:
    if liked_df.empty or cols_df.empty:
        st.error("No workbook yet. Click Collect first.")
    else:
        client = SketchfabClient()
        push(liked_df, cols_df, client, dry_run=dry_run)
        st.success("Push done (check log for details).")
