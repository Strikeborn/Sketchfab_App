# main.py — Flet desktop app (v6.2)
# Works with Flet 0.28.3. Keep this file in src/main.py
# -----------------------------------------------------
from __future__ import annotations

import os
import math
import re
from datetime import datetime
import sys
import contextlib
import pandas as pd
import flet as ft

# Flet 0.28.3+: prefer public modules; fall back when missing (older builds)
try:
    icons = ft.icons  # most builds
except Exception:  # pragma: no cover
    icons = getattr(ft, "Icons", None)
if not icons:
    class _Icons:
        CHECK = "check"
        CLOSE = "close"
        SAVE = "save"
        PLAYLIST_ADD_CHECK = "playlist_add_check"
        VERTICAL_ALIGN_TOP = "vertical_align_top"
        ARROW_FORWARD = "arrow_forward"
    icons = _Icons()

try:
    colors = ft.colors
except Exception:  # pragma: no cover
    class _Colors:
        GREEN_400 = "#66bb6a"
        RED_400 = "#ef5350"
        GREY_300 = "#e0e0e0"
        GREY_500 = "#9e9e9e"
        BLUE_300 = "#64b5f6"
        WHITE = "#ffffff"
    colors = _Colors()

# ---- Optional project modules (present in your repo) -----------------------
try:
    from collector import build_workbook
    from data_io import read_workbook, write_workbook, XL_PATH
    from matching import Terms
    from auto_assign import run_auto_assign
    from push_assignments import push
    from sketchfab_client import SketchfabClient
    from merge_collections import find_similar_collections
except Exception:  # keep the app booting even if imports fail in dev
    build_workbook = None
    read_workbook = None
    write_workbook = None
    XL_PATH = os.path.join("data", "sketchfab_data.xlsx")
    Terms = None
    run_auto_assign = None
    push = None
    SketchfabClient = None
    find_similar_collections = None

# ----------------------------------------------------------------------------
# Utility helpers
# ----------------------------------------------------------------------------

def none_or(v: object) -> str:
    if v is None:
        return "None"
    try:
        if pd.isna(v):
            return "None"
    except Exception:
        pass
    s = str(v)
    return s if s.strip() else "None"


def safe_bool(v: object) -> bool:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return False
        if isinstance(v, (int, bool)):
            return bool(v)
        return str(v).strip().lower() in {"1", "true", "yes", "y"}
    except Exception:
        return False


def series_nonempty(s: pd.Series) -> pd.Series:
    if s is None:
        return pd.Series([], dtype=bool)
    ss = s.astype(str).str.strip().str.lower()
    empty_mask = ss.isin(["", "none", "nan", "<na>"])
    empty_mask |= s.isna()
    return ~empty_mask


def slugify(name: str) -> str:
    """Create a collection slug like sketchfab expects (name-uid).
    Lowercase, spaces->hyphens, strip non-word chars, collapse dashes.
    """
    if not name:
        return "collection"
    s = re.sub(r"[^a-z0-9\-\s]", "", str(name).lower())
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "collection"


# ----------------------------------------------------------------------------
# Simple state container (no nonlocal / globals)
# ----------------------------------------------------------------------------
class AppState:
    def __init__(self):
        self.liked_df: pd.DataFrame | None = None
        self.cols_df: pd.DataFrame | None = None
        self.overwrite = False
        self.dry_run = True
        self.terms_topk = 100
        self.terms_minlen = 4
        self.terms_sample = 0  # 0 => ALL rows
        # paging for liked models
        self.liked_page = 0
        self.liked_page_size = 100
        # inline editing buffer for "Manual" assignments
        self.manual_edits: dict[str, str] = {}
        self.dirty = False  # needs save
        # username used for collection links
        self.username = os.environ.get("SKETCHFAB_USER", "").strip()
        # simple in-memory changelog
        self.logs: list[str] = []
        # top-right transient status text
        self.status: str = ""
        # viewport height for liked tab scroll area (computed at runtime)
        self.vh: int = 480

    # derived counts (safe)
    @property
    def liked_count(self) -> int:
        return 0 if self.liked_df is None else len(self.liked_df)

    @property
    def collections_count(self) -> int:
        return 0 if self.cols_df is None else len(self.cols_df)

    @property
    def assigned_count(self) -> int:
        if self.liked_df is None or self.liked_df.empty:
            return 0
        df = self.liked_df
        a = df.get("Already In Collection(s)", pd.Series(index=df.index, dtype="object"))
        c = df.get("Assigned Collection(s)", pd.Series(index=df.index, dtype="object"))
        mask = series_nonempty(a) | series_nonempty(c)
        return int(mask.sum())

    @property
    def pending_push_count(self) -> int:
        try:
            df = ensure_annotation_columns(self.liked_df if isinstance(self.liked_df, pd.DataFrame) else pd.DataFrame())
            if df is None or df.empty:
                return len(self.manual_edits or {})
            # Prefer new canonical name
            colname = "Assigned Collection(s)" if "Assigned Collection(s)" in df.columns else "Assigned Collection"
            mask_assigned = series_nonempty(df.get(colname, pd.Series(index=df.index, dtype="object")))
            pushed = df.get("Push Sent", pd.Series(index=df.index, dtype="object")).fillna(False)
            pending = mask_assigned & (~pushed.astype(bool))
            return int(pending.sum()) + (1 if (self.manual_edits or {}) else 0)
        except Exception:
            return 0

    @property
    def liked_pages(self) -> int:
        if self.liked_count == 0:
            return 0
        return int(math.ceil(self.liked_count / max(1, self.liked_page_size)))

    def liked_page_df(self) -> pd.DataFrame:
        if self.liked_df is None or self.liked_df.empty:
            return pd.DataFrame()
        start = max(0, self.liked_page) * max(1, self.liked_page_size)
        end = start + max(1, self.liked_page_size)
        return self.liked_df.iloc[start:end].copy()


# ----------------------------------------------------------------------------
# Persistence helpers
# ----------------------------------------------------------------------------
PERSIST_COLS = ["Manual", "Push Sent", "Pushed At"]


def ensure_annotation_columns(df: pd.DataFrame) -> pd.DataFrame:
    for col, default in ("Manual", "",), ("Push Sent", False,), ("Pushed At", ""):
        if col not in df.columns:
            df[col] = default
    # One-way migration to canonical names (drop legacy columns)
    try:
        if "Assigned Collection" in df.columns:
            if "Assigned Collection(s)" not in df.columns:
                df["Assigned Collection(s)"] = df["Assigned Collection"]
            del df["Assigned Collection"]
        if "Fuzzy Matched Collection(s)" in df.columns:
            if "Fuzzy Match Collection(s)" not in df.columns:
                df["Fuzzy Match Collection(s)"] = df["Fuzzy Matched Collection(s)"]
            del df["Fuzzy Matched Collection(s)"]
    except Exception:
        pass
    return df


def merge_preserve(prev: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Preserve manual annotations and push flags across a recollect.
    Match by UID.
    """
    if fresh is None or fresh.empty:
        return fresh
    fresh = fresh.copy()
    fresh = ensure_annotation_columns(fresh)
    if prev is None or prev.empty:
        return fresh
    prev = ensure_annotation_columns(prev)
    # map UID -> values
    m = prev.set_index("UID")[PERSIST_COLS].to_dict(orient="index")
    vals = fresh["UID"].map(lambda u: m.get(u, {}))
    # write back column-wise
    for col in PERSIST_COLS:
        fresh[col] = [(d.get(col) if isinstance(d, dict) else None) for d in vals]
        # normalize defaults
        if col == "Manual":
            fresh[col] = fresh[col].fillna("")
        elif col == "Push Sent":
            try:
                s = fresh[col].fillna(False)
                # future-safe: infer dtype then cast to bool
                s = s.infer_objects(copy=False)
                fresh[col] = s.astype(bool)
            except Exception:
                fresh[col] = fresh[col].fillna(False)
        elif col == "Pushed At":
            fresh[col] = fresh[col].fillna("")
    return fresh


# ----------------------------------------------------------------------------
# UI builders
# ----------------------------------------------------------------------------

_header_style = ft.TextStyle(size=13, weight=ft.FontWeight.BOLD)


def top_stat_line(page: ft.Page, state: AppState) -> ft.Row:
    xl = XL_PATH if os.path.exists(XL_PATH) else f"(missing) {XL_PATH}"
    token = os.environ.get("SKETCHFAB_TOKEN")
    pending = state.pending_push_count
    ok = (pending == 0) and (not state.dirty)
    ico = icons.CHECK if ok else icons.CLOSE
    col = colors.GREEN_400 if ok else colors.RED_400
    lbl = "Synced" if ok else f"Needs push ({pending})"
    parts = [
        ft.Text(f"Workbook: {xl}", size=13, color=colors.GREY_300),
        ft.Text("—"),
        ft.Text(f"Token: {'set' if token else 'missing'}", size=13, color=colors.GREY_300),
        ft.Text("—"),
        ft.Text(f"Liked: {state.liked_count:,}", size=13),
        ft.Text("—"),
        ft.Text(f"In Collections: {state.assigned_count:,}", size=13),
        ft.Text("—"),
        ft.Text(f"Collections: {state.collections_count:,}", size=13),
        ft.Text("—"),
        ft.Icon(name=ico, color=col, size=18),
        ft.Text(lbl, size=12, color=col),
    ]
    if state.username:
        parts.extend([ft.Text("—"), ft.Text(f"User: {state.username}", size=12, color=colors.GREY_300)])
    return ft.Row(controls=parts, alignment=ft.MainAxisAlignment.START, spacing=12)


def toolbar_row(page: ft.Page, state: AppState,
                on_collect, on_match, on_auto, on_push, on_apply, on_save) -> ft.Row:
    sw_over = ft.Switch(label="Overwrite Assigned", value=state.overwrite)
    sw_dry = ft.Switch(label="Dry-run Push", value=state.dry_run)

    sw_over.on_change = lambda e: setattr(state, "overwrite", bool(sw_over.value))
    sw_dry.on_change = lambda e: setattr(state, "dry_run", bool(sw_dry.value))

    return ft.Row(
        controls=[
            ft.ElevatedButton("Collect", on_click=on_collect),
            ft.OutlinedButton("Match", on_click=on_match),
            sw_over,
            ft.OutlinedButton("Auto-Assign", on_click=on_auto),
            sw_dry,
            ft.ElevatedButton("Push Assignments", on_click=on_push, icon=icons.PLAYLIST_ADD_CHECK),
            ft.OutlinedButton("Apply Manual", on_click=on_apply),
            ft.FilledTonalButton("Save Workbook", on_click=on_save, icon=icons.SAVE),
        ],
        alignment=ft.MainAxisAlignment.START,
        spacing=14,
        wrap=False,
    )


# --- Data table builders -----------------------------------------------------

def _hdr(label: str) -> ft.DataColumn:
    # Short labels with manual wrapping using "" to avoid overlap
    return ft.DataColumn(ft.Text(label, style=_header_style, no_wrap=False, max_lines=2, text_align=ft.TextAlign.CENTER))


def name_link_cell(name: str, uid: str) -> ft.DataCell:
    url = f"https://sketchfab.com/models/{uid}"
    return ft.DataCell(ft.TextButton(text=none_or(name), url=url))


def collection_link_cell(col_name: str, col_uid: str, username: str | None) -> ft.DataCell:
    username = (username or "").strip()
    slug = slugify(col_name)
    url = (
        f"https://sketchfab.com/{username}/collections/{slug}-{col_uid}"
        if username
        else f"https://sketchfab.com/collections/{slug}-{col_uid}"
    )
    return ft.DataCell(ft.TextButton(text=none_or(col_name), url=url))


def bool_badge_cell(v: object) -> ft.DataCell:
    ok = safe_bool(v)
    icon = icons.CHECK if ok else icons.CLOSE
    col = colors.GREEN_400 if ok else colors.RED_400
    return ft.DataCell(ft.Icon(name=icon, color=col, size=18))


def text_cell(v: object) -> ft.DataCell:
    s = none_or(v)
    c = colors.GREY_500 if s == "None" else None
    return ft.DataCell(ft.Text(s, color=c))


def _column_spacing_for(page: ft.Page) -> int:
    try:
        w = page.width or 1400
    except Exception:
        w = 1400
    if w < 1100:
        return 18
    if w < 1400:
        return 24
    return 28


def build_liked_table(page: ft.Page, state: AppState, df: pd.DataFrame, row_offset: int = 0) -> ft.DataTable:
    cols = [
        _hdr("#"),
        _hdr("Name"),
        _hdr("UID"),
        _hdr("Assigned"),
        _hdr("Manual"),
        _hdr("Already\nIn"),
        _hdr("Auto\nAssigned"),
        _hdr("Suggested"),
        _hdr("Fuzzy\nMatch"),
        _hdr("Author"),
        _hdr("License"),
        _hdr("DL"),
        _hdr("Tags"),
    ]

    rows: list[ft.DataRow] = []
    if df is not None and not df.empty:
        for i in range(len(df)):
            r = df.iloc[i]
            uid = str(r.get("UID", ""))
            # manual value resolves to buffer -> column -> ''
            manual_val = state.manual_edits.get(uid, str(r.get("Manual", "")))
            tf = ft.TextField(value=manual_val, dense=True, width=360)

            def _mk_on_change(the_uid: str, field: ft.TextField):
                def _handler(e):
                    state.manual_edits[the_uid] = field.value or ""
                    state.dirty = True
                return _handler
            tf.on_change = _mk_on_change(uid, tf)

            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(str(row_offset + i + 1))),
                        name_link_cell(r.get("Name") or r.get("Model Name"), uid),
                        text_cell(uid),
                        text_cell(r.get("Assigned Collection(s)")),
                        ft.DataCell(tf),
                        text_cell(r.get("Already In Collection(s)")),
                        text_cell(r.get("Auto-Assigned Collection(s)")),
                        text_cell(r.get("Suggested Collection(s)")),
                        text_cell(r.get("Fuzzy Match Collection(s)")),
                        text_cell(r.get("Author")),
                        text_cell(r.get("License")),
                        bool_badge_cell(r.get("Downloadable")),
                        text_cell(r.get("Tags")),
                    ]
                )
            )

    return ft.DataTable(
        columns=cols,
        rows=rows,
        column_spacing=_column_spacing_for(page),
        heading_row_height=54,
        data_row_max_height=46,
    )


def build_collections_table(page: ft.Page, df: pd.DataFrame, username: str | None) -> ft.DataTable:
    cols = [
        ft.DataColumn(ft.Text("#", style=_header_style, no_wrap=True)),
        ft.DataColumn(ft.Text("Collection Name", style=_header_style, no_wrap=False, max_lines=2)),
        ft.DataColumn(ft.Text("UID", style=_header_style, no_wrap=True)),
        ft.DataColumn(ft.Text("Models", style=_header_style, no_wrap=True)),
    ]
    rows: list[ft.DataRow] = []
    if df is not None and not df.empty:
        for i in range(len(df)):
            r = df.iloc[i]
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(str(i + 1))),
                        collection_link_cell(r.get("Collection Name"), r.get("Collection UID"), username),
                        text_cell(r.get("Collection UID")),
                        text_cell(r.get("Model Count")),
                    ]
                )
            )
    return ft.DataTable(columns=cols, rows=rows, column_spacing=_column_spacing_for(page), heading_row_height=40, data_row_max_height=44)


# --- Terms / report ----------------------------------------------------------

def mine_terms(df: pd.DataFrame, top_k: int, min_len: int, sample_n: int | None) -> pd.DataFrame:
    from collections import Counter

    out = Counter()
    if df is None or df.empty:
        return pd.DataFrame(columns=["term", "count"])
    if sample_n and int(sample_n) > 0:
        df = df.head(int(sample_n))  # else use ALL

    for _, r in df.iterrows():
        name = (r.get("Name") or r.get("Model Name") or "")
        tags = str(r.get("Tags") or "")
        for t in re.findall(r"[a-z0-9]+", (str(name) + " " + tags).lower()):
            if len(t) >= int(min_len):
                out[t] += 1
    pairs = sorted(out.items(), key=lambda x: x[1], reverse=True)[: int(top_k)]
    return pd.DataFrame(pairs, columns=["term", "count"]) if pairs else pd.DataFrame(columns=["term", "count"]) 


def build_terms_view(page: ft.Page, state: AppState) -> ft.Row:
    # Left: table, Right: sticky YAML builder (non-overlapping)
    topk_val = ft.Text(f"Top K: {state.terms_topk}", size=12)
    minlen_val = ft.Text(f"Min len: {state.terms_minlen}", size=12)
    sample_val = ft.Text(f"Sample N: {'ALL' if state.terms_sample <= 0 else state.terms_sample}", size=12)

    # sliders — compact block (about ~25% shorter) in a dark card "bubble"
    s_topk = ft.Slider(min=10, max=300, divisions=29, value=float(state.terms_topk))
    s_minl = ft.Slider(min=3, max=8, divisions=5, value=float(state.terms_minlen))

    def compute_pending():
        if state.liked_df is not None and not state.liked_df.empty:
            df = state.liked_df
            a = df.get("Already In Collection(s)", pd.Series(index=df.index, dtype="object"))
            c = df.get("Assigned Collection(s)", pd.Series(index=df.index, dtype="object"))
            mask = ~(series_nonempty(a) | series_nonempty(c))
            return df.loc[mask]
        return pd.DataFrame()

    pending0 = compute_pending()
    max_sample = max(500, len(pending0))
    s_samp = ft.Slider(min=0, max=float(max_sample), divisions=50, value=float(state.terms_sample))

    def sync_labels_from_sliders():
        state.terms_topk = int(s_topk.value)
        state.terms_minlen = int(s_minl.value)
        state.terms_sample = int(s_samp.value)
        topk_val.value = f"Top K: {state.terms_topk}"
        minlen_val.value = f"Min len: {state.terms_minlen}"
        sample_val.value = f"Sample N: {'ALL' if state.terms_sample <= 0 else state.terms_sample}"

    def make_rows(df_terms: pd.DataFrame) -> list:
        rows = []
        for i, r in df_terms.reset_index(drop=True).iterrows():
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(str(i + 1))),
                ft.DataCell(ft.Text(str(r.term))),
                ft.DataCell(ft.Text(str(r['count']))),
            ]))
        return rows

    terms_df = mine_terms(pending0, state.terms_topk, state.terms_minlen, (state.terms_sample or None))
    terms_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("#", style=_header_style, no_wrap=True)),
            ft.DataColumn(ft.Text("term", style=_header_style, no_wrap=True)),
            ft.DataColumn(ft.Text("count", style=_header_style, no_wrap=True)),
        ],
        rows=make_rows(terms_df),
        column_spacing=_column_spacing_for(page),
        heading_row_height=40,
    )

    def update_terms(_=None):
        sync_labels_from_sliders()
        df_new = mine_terms(compute_pending(), state.terms_topk, state.terms_minlen, (state.terms_sample or None))
        terms_table.rows = make_rows(df_new)
        page.update()

    for sl in (s_topk, s_minl, s_samp):
        sl.on_change_end = update_terms

    sliders_row = ft.Container(
        content=ft.Row([
            ft.Column([topk_val, s_topk], spacing=2),
            ft.Column([minlen_val, s_minl], spacing=2),
            ft.Column([sample_val, s_samp], spacing=2),
        ], spacing=16),
        padding=10,
        bgcolor="#0f172a",
        border_radius=8,
    )

    # Right sticky YAML panel (dark to match theme)
    tf_target = ft.TextField(label="Target collection", dense=True)
    tf_terms = ft.TextField(label="Picked terms (comma-separated)", multiline=True, min_lines=4, max_lines=6)
    yaml_out = ft.Text(value="# fill target and terms", selectable=True, size=12)

    def build_yaml(e=None):
        target = (tf_target.value or "").strip()
        picked = [t.strip() for t in (tf_terms.value or "").split(",") if t.strip()]
        if not target or not picked:
            yaml_out.value = "# fill target and terms"
        else:
            yaml_out.value = "\n".join([
                f"{target}:",
                f"  include_terms: [{', '.join(picked)}]",
                f"  # tag_terms: [{', '.join(picked)}]",
                "  exclude_terms: []",
                "  fuzzy_threshold: 88",
            ])
        page.update()

    btn_yaml = ft.OutlinedButton("Build YAML", on_click=build_yaml)

    # Centered table with its own scrollbar
    left_panel = ft.Container(
        content=ft.Column([
            sliders_row,
            ft.Row([terms_table], alignment=ft.MainAxisAlignment.CENTER),
        ], spacing=10, expand=True, scroll=ft.ScrollMode.ALWAYS),
        expand=True,
    )

    right_panel = ft.Container(
        content=ft.Column([
            ft.Text("YAML Builder", weight=ft.FontWeight.BOLD),
            tf_target,
            tf_terms,
            btn_yaml,
            ft.Divider(),
            yaml_out,
        ], spacing=10),
        width=340,
        padding=12,
        bgcolor="#0f172a",
        border_radius=8,
    )

    return ft.Row([left_panel, right_panel], spacing=20, vertical_alignment=ft.CrossAxisAlignment.START, expand=True)


# ----------------------------------------------------------------------------
# Page (app) entry
# ----------------------------------------------------------------------------

def main(page: ft.Page):
    page.title = "Sketchfab Collections — v6.2"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.HIDDEN

    state = AppState()
    # Override title/scroll with safe values
    page.title = "Sketchfab Collections v6.2"
    page.scroll = ft.ScrollMode.HIDDEN

    # Load data if workbook exists
    if read_workbook and os.path.exists(XL_PATH):
        try:
            liked, cols = read_workbook()
            liked = ensure_annotation_columns(liked if isinstance(liked, pd.DataFrame) else pd.DataFrame())
            state.liked_df = liked
            state.cols_df = cols
        except Exception as ex:
            page.snack_bar = ft.SnackBar(ft.Text(f"Failed to read workbook: {ex}"))
            page.snack_bar.open = True
    else:
        state.liked_df = ensure_annotation_columns(pd.DataFrame())
        state.cols_df = pd.DataFrame()

    # Top status + toolbar ----------------------------------------------------
    header = top_stat_line(page, state)
    status_text = ft.Text(state.status or "", size=12, color=colors.BLUE_300)
    status_bar = ft.Row([ft.Container(expand=1), status_text], alignment=ft.MainAxisAlignment.START)

    # Compute initial viewport height for liked list (ensures a visible scrollbar)
    def compute_vh() -> int:
        try:
            h = page.window_height or page.height or 900
        except Exception:
            h = 900
        # subtract approximate header + status + toolbar + tabs header
        return max(400, int(h - 140))

    state.vh = compute_vh()

    # Simple in-memory changelog helper
    def add_log(msg: str):
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            state.logs.append(f"[{ts}] {msg}")
            if len(state.logs) > 200:
                state.logs = state.logs[-200:]
        except Exception:
            pass

    def save_workbook_silent(msg: str | None = None):
        if write_workbook and isinstance(state.liked_df, pd.DataFrame):
            try:
                ensure_annotation_columns(state.liked_df)
                write_workbook(state.liked_df, (state.cols_df if isinstance(state.cols_df, pd.DataFrame) else pd.DataFrame()))
                state.dirty = False
                if msg:
                    page.snack_bar = ft.SnackBar(ft.Text(msg))
                    page.snack_bar.open = True
                add_log(msg or "Saved workbook")
            except Exception as ex:
                page.snack_bar = ft.SnackBar(ft.Text(f"Save error: {ex}"))
                page.snack_bar.open = True
                add_log(f"Save error: {ex}")
        page.update()

    # --- handlers for toolbar buttons ---------------------------------------
    def do_collect(e):
        if not build_workbook:
            page.snack_bar = ft.SnackBar(ft.Text("collector module not available"))
            page.snack_bar.open = True
            page.update()
            return
        prev = state.liked_df.copy() if isinstance(state.liked_df, pd.DataFrame) else pd.DataFrame()
        try:
            # Stream collector prints into top-right status
            class _StatusWriter:
                def __init__(self, setter):
                    self._buf = ""
                    self._setter = setter
                def write(self, s: str):
                    if not isinstance(s, str):
                        s = str(s)
                    self._buf += s
                    while "\n" in self._buf:
                        line, self._buf = self._buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._setter(line)
                def flush(self):
                    pass

            def _set_status(msg: str):
                state.status = msg
                status_text.value = msg
                header.controls = top_stat_line(page, state).controls
                page.update()

            sw = _StatusWriter(_set_status)
            with contextlib.redirect_stdout(sw):
                path = build_workbook()
            liked, cols = read_workbook()
            liked = merge_preserve(prev, liked)
            state.liked_df, state.cols_df = liked, cols
            header.controls = top_stat_line(page, state).controls
            render_all_tabs()
            page.snack_bar = ft.SnackBar(ft.Text(f"Workbook built: {path} (manual & push flags preserved)"))
            page.snack_bar.open = True
            add_log("Collected workbook (likes + collections)")
            final_msg = f"Collected: {state.liked_count} likes / {state.collections_count} collections"
            state.status = final_msg
            status_text.value = final_msg
        except Exception as ex:
            page.snack_bar = ft.SnackBar(ft.Text(f"Collect error: {ex}"))
            page.snack_bar.open = True
        page.update()

    def do_match(e):
        if state.liked_df is None or state.liked_df.empty:
            page.snack_bar = ft.SnackBar(ft.Text("No workbook yet."))
            page.snack_bar.open = True
            page.update()
            return
        if Terms is None or run_auto_assign is None:
            page.snack_bar = ft.SnackBar(ft.Text("matching module not available"))
            page.snack_bar.open = True
            page.update()
            return
        try:
            terms = Terms.from_yaml(os.environ.get("TERMS_PATH", os.path.join("terms", "collections_terms.yaml")))
            updated = run_auto_assign(state.liked_df.copy(), terms, overwrite=False)
            # keep manual edits & push flags
            updated = merge_preserve(state.liked_df, updated)
            if write_workbook:
                write_workbook(updated, (state.cols_df if isinstance(state.cols_df, pd.DataFrame) else pd.DataFrame()))
            state.liked_df = updated
            header.controls = top_stat_line(page, state).controls
            render_all_tabs()
            page.update()
            try:
                add_log("Computed suggestions/fuzzy (Match)")
            except Exception:
                pass
        except Exception as ex:
            page.snack_bar = ft.SnackBar(ft.Text(f"Match error: {ex}"))
            page.snack_bar.open = True
            page.update()

    def do_auto(e):
        if state.liked_df is None or state.liked_df.empty or run_auto_assign is None or Terms is None:
            page.snack_bar = ft.SnackBar(ft.Text("Auto-assign prerequisites missing"))
            page.snack_bar.open = True
            page.update()
            return
        try:
            terms = Terms.from_yaml(os.environ.get("TERMS_PATH", os.path.join("terms", "collections_terms.yaml")))
            updated = run_auto_assign(state.liked_df.copy(), terms, overwrite=state.overwrite)
            updated = merge_preserve(state.liked_df, updated)
            if write_workbook:
                write_workbook(updated, (state.cols_df if isinstance(state.cols_df, pd.DataFrame) else pd.DataFrame()))
            state.liked_df = updated
            header.controls = top_stat_line(page, state).controls
            render_all_tabs()
            page.update()
            try:
                add_log(f"Auto-assign complete (overwrite={state.overwrite})")
            except Exception:
                pass
        except Exception as ex:
            page.snack_bar = ft.SnackBar(ft.Text(f"Auto-assign error: {ex}"))
            page.snack_bar.open = True
            page.update()

    def do_push(e):
        if push is None or SketchfabClient is None:
            page.snack_bar = ft.SnackBar(ft.Text("push modules not available"))
            page.snack_bar.open = True
            page.update()
            return
        try:
            client = SketchfabClient()
            # rows that actually have an Assigned Collection to push
            df = state.liked_df if isinstance(state.liked_df, pd.DataFrame) else pd.DataFrame()
            df = ensure_annotation_columns(df)
            mask = series_nonempty(df.get("Assigned Collection(s)", pd.Series(index=df.index, dtype="object")))
            rows_to_push = df.loc[mask].copy()

            # Adapter: map UI/collector schema -> pipeline push schema
            # push() expects columns: "Model UID", "Assigned Collection(s)"
            # Our UI uses:            "UID" (rename), Assigned name already matches
            if not rows_to_push.empty:
                cols_map = {}
                if "UID" in rows_to_push.columns:
                    cols_map["UID"] = "Model UID"
                df_push = rows_to_push.rename(columns=cols_map)
            else:
                df_push = rows_to_push

            push(
                df_push,
                (state.cols_df if isinstance(state.cols_df, pd.DataFrame) else pd.DataFrame()),
                client,
                dry_run=state.dry_run,
            )
            if not state.dry_run:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                df.loc[mask, "Push Sent"] = True
                df.loc[mask, "Pushed At"] = ts
                state.liked_df = df
                state.dirty = True
                save_workbook_silent("Pushed & saved")
            else:
                page.snack_bar = ft.SnackBar(ft.Text("Dry-run complete (no flags updated)"))
                page.snack_bar.open = True
            try:
                add_log(f"Push {'dry-run' if state.dry_run else 'sent'}: {int(mask.sum())} assignments")
            except Exception:
                pass
            # Refresh table so the view is in sync
            header.controls = top_stat_line(page, state).controls
            render_liked_tab()
        except Exception as ex:
            page.snack_bar = ft.SnackBar(ft.Text(f"Push error: {ex}"))
            page.snack_bar.open = True

            try:
                add_log(f"Push error: {ex}")
            except Exception:
                pass
        page.update()

    def do_apply_manual(e):
        df = state.liked_df if isinstance(state.liked_df, pd.DataFrame) else pd.DataFrame()
        if df is None or df.empty:
            return
        df = ensure_annotation_columns(df)
        # write manual buffer to column and Assigned Collection(s)
        changed = 0
        for uid, val in list(state.manual_edits.items()):
            idx = df.index[df["UID"].astype(str) == str(uid)]
            if len(idx) > 0:
                df.loc[idx, "Manual"] = val
                # keep manual separate, but also set the effective assigned slot
                if val.strip():
                    df.loc[idx, "Assigned Collection(s)"] = val.strip()
                changed += 1
        if changed:
            state.liked_df = df
            state.dirty = True
            header.controls = top_stat_line(page, state).controls
            render_liked_tab()
            page.update()
            try:
                add_log(f"Applied manual edits: {changed} row(s)")
            except Exception:
                pass

    def do_save(e):
        save_workbook_silent("Saved")

    toolbar = toolbar_row(page, state, do_collect, do_match, do_auto, do_push, do_apply_manual, do_save)

    # Tabs --------------------------------------------------------------------
    liked_tab = ft.Container(expand=True)
    cols_tab = ft.Container(expand=True)
    report_tab = ft.Container(expand=True)
    log_tab = ft.Container(expand=True)

    tabs = ft.Tabs(
        selected_index=0,
        tabs=[
            ft.Tab(text="Liked Models", content=liked_tab),
            ft.Tab(text="Collections", content=cols_tab),
            ft.Tab(text="Report / Terms", content=report_tab),
            ft.Tab(text="Changelog", content=log_tab),
        ],
        expand=1,
    )

    # Floating "Top" button (instant)
    def scroll_top(e):
        try:
            page.scroll_to(offset=0, duration=0)
        except Exception:
            page.scroll_to(0)
    page.floating_action_button = ft.FloatingActionButton(icon=icons.VERTICAL_ALIGN_TOP, on_click=scroll_top)

    # Auto-save on window close
    page.window_prevent_close = False
    def on_window_event(e: ft.WindowEvent):
        if e.data == "close" and state.dirty:
            save_workbook_silent("Auto-saved on close")
            try:
                add_log("Auto-saved on window close")
            except Exception:
                pass
    page.on_window_event = on_window_event

    # Update viewport height on resize to keep liked tab scroll working
    def on_resize(e: ft.ControlEvent):
        try:
            state.vh = compute_vh()
            # Rerender all tabs so each viewport height updates
            render_liked_tab()
            render_cols_tab()
            render_report_tab()
            page.update()
        except Exception:
            pass
    try:
        page.on_resize = on_resize
    except Exception:
        pass

    # Helper to create a centered container with horizontal scroll for wide tables
    def centered_table_container(child: ft.Control) -> ft.Column:
        # Center the table; keep horizontal scrollbar visible when needed
        return ft.Column([
            ft.Row([child], alignment=ft.MainAxisAlignment.CENTER, scroll=ft.ScrollMode.ALWAYS)
        ], expand=False)

    # Renderers ---------------------------------------------------------------
    def render_liked_tab():
        df_all = state.liked_df if isinstance(state.liked_df, pd.DataFrame) else pd.DataFrame()
        df_all = ensure_annotation_columns(df_all)
        if df_all is None or df_all.empty:
            liked_tab.content = ft.Column([ft.Text("No liked models loaded")])
            page.update()
            return

        # clamp current page
        if state.liked_pages > 0:
            state.liked_page = max(0, min(state.liked_page, state.liked_pages - 1))
        df_page = state.liked_page_df()

        table = build_liked_table(page, state, df_page, row_offset=state.liked_page * state.liked_page_size)

        # --- Page slider navigation -----------------------------------------
        pages = max(1, state.liked_pages)
        page_label = ft.Text(f"Page {state.liked_page + 1} / {pages}")
        s_page = ft.Slider(min=1, max=float(pages), divisions=max(0, pages - 1), value=float(state.liked_page + 1))

        def on_slide(e):
            page_label.value = f"Page {int(s_page.value)} / {pages}"
            page.update()
        def on_slide_end(e):
            # move to the selected page only when the user releases the thumb
            state.liked_page = int(s_page.value) - 1
            render_liked_tab()

        s_page.on_change = on_slide
        s_page.on_change_end = on_slide_end

        def on_size_change(e):
            try:
                state.liked_page_size = int(size_dd.value)
            except Exception:
                state.liked_page_size = 300
            state.liked_page = 0
            render_liked_tab()

        size_dd = ft.Dropdown(
            label="Rows",
            value=str(state.liked_page_size),
            options=[ft.dropdown.Option(str(n)) for n in (100, 300, 500, 1000)],
            width=110,
            on_change=on_size_change,
        )

        nav = ft.Row([
            page_label,
            ft.Container(width=280, content=s_page),  # keep slider compact
            size_dd,
        ], spacing=12, alignment=ft.MainAxisAlignment.START)

        liked_tab.content = ft.Container(
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=8,
                controls=[
                    nav,
                    ft.ListView(
                        expand=True,
                        spacing=0,
                        controls=[centered_table_container(table)],
                    ),
                ],
            ),
        )
        page.update()

    def render_cols_tab():
        # Merge UI + Username on the RIGHT panel
        names = []
        if isinstance(state.cols_df, pd.DataFrame) and not state.cols_df.empty:
            try:
                names = sorted(list(state.cols_df.get("Collection Name", pd.Series(dtype=str)).dropna().astype(str).unique()))
            except Exception:
                names = []
        dd_from = ft.Dropdown(label="From", options=[ft.dropdown.Option(n) for n in names], width=260)
        dd_into = ft.Dropdown(label="Into", options=[ft.dropdown.Option(n) for n in names], width=260)

        def do_merge_click(e):
            src = (dd_from.value or "").strip()
            dst = (dd_into.value or "").strip()
            if not src or not dst or src.lower() == dst.lower():
                page.snack_bar = ft.SnackBar(ft.Text("Pick two different collections to merge"))
                page.snack_bar.open = True
                page.update()
                return
            df = ensure_annotation_columns(state.liked_df if isinstance(state.liked_df, pd.DataFrame) else pd.DataFrame())
            if df is None or df.empty:
                return
            a = df.get("Assigned Collection(s)", pd.Series(index=df.index, dtype="object")).astype(str).str.strip().str.lower() == src.lower()
            m = df.get("Manual", pd.Series(index=df.index, dtype="object")).astype(str).str.strip().str.lower() == src.lower()
            df.loc[a, "Assigned Collection(s)"] = dst
            df.loc[m, "Manual"] = dst
            state.liked_df = df
            state.dirty = True
            header.controls = top_stat_line(page, state).controls
            render_liked_tab()
            page.snack_bar = ft.SnackBar(ft.Text(f"Merged '{src}' into '{dst}' locally. Save or Push to sync."))
            page.snack_bar.open = True
            page.update()

        merge_row = ft.Row([
            dd_from,
            ft.Icon(name=icons.ARROW_FORWARD),
            dd_into,
            ft.ElevatedButton("Merge", on_click=do_merge_click),
        ], spacing=12)

        # Similar suggestions with quick fill
        suggest_block = ft.Column(spacing=4)
        try:
            if callable(find_similar_collections) and isinstance(state.cols_df, pd.DataFrame) and not state.cols_df.empty:
                pairs = find_similar_collections(state.cols_df)
                if pairs:
                    for a, b, score in pairs[:8]:
                        def make_fill(sa=a, sb=b):
                            def _f(_):
                                dd_from.value, dd_into.value = sa, sb
                                page.update()
                            return _f
                        suggest_block.controls.append(
                            ft.Row([
                                ft.Text(f"Similar: '{a}' → '{b}'  ({score*100:.0f}%)"),
                                ft.TextButton("Fill", on_click=make_fill()),
                            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
                        )
        except Exception:
            pass

        # Username setter for correct collection URLs (above the merge box)
        tf_user = ft.TextField(label="Sketchfab Username", value=state.username, dense=True)
        def save_user(e):
            state.username = (tf_user.value or "").strip()
            os.environ["SKETCHFAB_USER"] = state.username
            header.controls = top_stat_line(page, state).controls
            render_cols_tab()  # refresh links
            page.update()
            try:
                add_log("Updated Sketchfab username")
            except Exception:
                pass
        btn_user = ft.OutlinedButton("Save", on_click=save_user)
        username_card = ft.Container(
            content=ft.Row([tf_user, btn_user], spacing=8),
            padding=12,
            bgcolor="#0f172a",
            border_radius=8,
            width=420,
        )

        table = build_collections_table(page, (state.cols_df if isinstance(state.cols_df, pd.DataFrame) else pd.DataFrame()), state.username)

        right_panel = ft.Container(
            content=ft.Column([
                ft.Text("Merge Collections", weight=ft.FontWeight.BOLD),
                merge_row,
                (suggest_block if suggest_block.controls else ft.Text(" ")),
            ], spacing=10),
            width=420,  # a bit wider so both dropdowns are fully visible
            padding=12,
            bgcolor="#0f172a",
            border_radius=8,
        )

        left_panel = ft.Container(
            expand=True,
            content=ft.ListView(expand=True, spacing=0, controls=[centered_table_container(table)]),
        )
        # Keep the right panel closer to the table (no pushing to the far right)
        cols_tab.content = ft.Row([left_panel, ft.Column([username_card, right_panel], spacing=10)], spacing=20, alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.START, expand=True)

    def render_report_tab():
        # Ensure this tab also has a vertical scrollbar
        report_tab.content = ft.Container(
            expand=True,
            content=ft.ListView(expand=True, spacing=0, controls=[build_terms_view(page, state)]),
        )
    def render_log_tab():
        items = [ft.Text(s, size=12) for s in reversed(state.logs)] if state.logs else [ft.Text("No events yet.")]
        log_tab.content = ft.Container(content=ft.Column(items, spacing=4, scroll=ft.ScrollMode.ALWAYS), padding=10)

    def render_all_tabs():
        render_liked_tab()
        render_cols_tab()
        render_report_tab()
        render_log_tab()

    # Assemble page -----------------------------------------------------------
    root = ft.Column(
        expand=True,
        controls=[
            header,
            status_bar,
            toolbar,
            ft.Divider(),
            ft.Container(expand=True, content=tabs),
        ],
    )
    page.add(root)
    render_all_tabs()
    page.update()


if __name__ == "__main__":
    ft.app(target=main)
