from __future__ import annotations
import os
import re
import shutil
import logging
import threading
from datetime import datetime, timezone

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from state import normalize_liked_columns, enrich_download_status

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uFFFE\uFFFF]")

DATA_DIR = os.environ.get("DATA_DIR", "data")
XL_PATH = os.path.join(DATA_DIR, "sketchfab_data.xlsx")

LIKED_SHEET = "Liked Models"
COLL_SHEET = "Collections"
SUBS_SHEET = "Subscribed Collections"
BROWSE_SHEET = "Browse Results"
SUBS_COLUMNS = ["Collection Name", "Collection UID", "Owner", "Owner Profile", "Model Count"]
BROWSE_COLUMNS = [
    "Name", "UID", "Author", "Views", "Likes", "License", "Categories",
    "Face Count", "Downloadable", "Thumbnail", "Viewer URL", "Found At", "Query",
]

os.makedirs(DATA_DIR, exist_ok=True)


def _sanitize_excel_value(val):
    if isinstance(val, str):
        return _ILLEGAL_XML_RE.sub("", val)
    return val


def _sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object or pd.api.types.is_string_dtype(out[col]):
            out[col] = out[col].map(_sanitize_excel_value)
    return out


def _backup_workbook(path: str) -> None:
    bak = path + ".bak"
    if not os.path.exists(path):
        return
    try:
        shutil.copy2(path, bak)
    except OSError as exc:
        logger.warning("Could not back up workbook to %s: %s", bak, exc)


def write_workbook(
    liked_df: pd.DataFrame,
    collections_df: pd.DataFrame,
    subscribed_df: pd.DataFrame | None = None,
    browse_df: pd.DataFrame | None = None,
) -> str:
    with _WRITE_LOCK:
        liked_out = _sanitize_df(normalize_liked_columns(liked_df.copy()))

        if "Tags" in liked_out.columns:
            try:
                liked_out["Tags"] = liked_out["Tags"].apply(
                    lambda x: ", ".join(x) if isinstance(x, list) else (x or "")
                )
            except Exception:
                pass

        cols_out = _sanitize_df(collections_df.copy())
        if subscribed_df is not None:
            subs_out = _sanitize_df(subscribed_df.copy())
        elif os.path.exists(XL_PATH):
            try:
                x = pd.ExcelFile(XL_PATH)
                subs_out = x.parse(SUBS_SHEET) if SUBS_SHEET in x.sheet_names else pd.DataFrame()
            except Exception:
                subs_out = pd.DataFrame()
        else:
            subs_out = pd.DataFrame()

        if subs_out.empty:
            subs_out = pd.DataFrame(columns=SUBS_COLUMNS)
        else:
            subs_out = _sanitize_df(subs_out)

        # Avoid re-reading the whole xlsx just to preserve Browse when caller didn't pass it —
        # only load browse if the file exists and browse_df is explicitly None.
        if browse_df is not None:
            browse_out = _sanitize_df(browse_df)
        else:
            browse_out = _sanitize_df(_read_browse_sheet_raw())

        tmp_path = XL_PATH.replace(".xlsx", ".write.xlsx")
        try:
            with pd.ExcelWriter(tmp_path, engine="openpyxl") as xw:
                liked_out.to_excel(xw, index=False, sheet_name=LIKED_SHEET)
                cols_out.to_excel(xw, index=False, sheet_name=COLL_SHEET)
                subs_out.to_excel(xw, index=False, sheet_name=SUBS_SHEET)
                browse_out.to_excel(xw, index=False, sheet_name=BROWSE_SHEET)

            # Skip openpyxl re-open/finalize — doubles write time on large Liked sheets.
            # Freeze panes / widths are cosmetic; Collect path already formats its own sheets.
            _backup_workbook(XL_PATH)
            os.replace(tmp_path, XL_PATH)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
        return XL_PATH


def _finalize_workbook(path: str, sheets: list[str]) -> None:
    """Light post-pass: freeze header only. Full width/empty scans are too slow on large likes sheets."""
    try:
        wb = load_workbook(path)
    except Exception as exc:
        logger.warning("Skipping workbook finalize (could not read %s): %s", path, exc)
        return
    try:
        for name in sheets:
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            ws.freeze_panes = "A2"
            # Cheap widths from header row only.
            for c in range(1, (ws.max_column or 1) + 1):
                header = str(ws.cell(row=1, column=c).value or "")
                ws.column_dimensions[get_column_letter(c)].width = min(max(len(header) + 2, 12), 40)
        wb.save(path)
    except Exception as exc:
        logger.warning("Skipping workbook finalize (post-process failed): %s", exc)


def read_workbook(scan_downloads: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not os.path.exists(XL_PATH):
        raise FileNotFoundError(f"Workbook not found: {XL_PATH}")
    x = pd.ExcelFile(XL_PATH)
    liked = normalize_liked_columns(x.parse(LIKED_SHEET))
    cols = x.parse(COLL_SHEET)
    subs = x.parse(SUBS_SHEET) if SUBS_SHEET in x.sheet_names else pd.DataFrame()
    if scan_downloads:
        liked = enrich_download_status(liked)
    return liked, cols, subs


def _read_browse_sheet_raw() -> pd.DataFrame:
    if not os.path.exists(XL_PATH):
        return pd.DataFrame(columns=BROWSE_COLUMNS)
    try:
        x = pd.ExcelFile(XL_PATH)
        if BROWSE_SHEET not in x.sheet_names:
            return pd.DataFrame(columns=BROWSE_COLUMNS)
        df = x.parse(BROWSE_SHEET)
        for c in BROWSE_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        return df
    except Exception:
        return pd.DataFrame(columns=BROWSE_COLUMNS)


def read_browse_results() -> pd.DataFrame:
    return _read_browse_sheet_raw()


def append_browse_results(rows: list[dict], query: str = "") -> tuple[str, int]:
    """Append browse rows to workbook sheet. Returns (path, new_row_count)."""
    if not rows:
        return XL_PATH, 0
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    existing = _read_browse_sheet_raw()
    new_rows = []
    for r in rows:
        new_rows.append({
            "Name": r.get("Name") or "",
            "UID": r.get("UID") or "",
            "Author": r.get("Author") or "",
            "Views": r.get("viewCount") or r.get("Views") or "",
            "Likes": r.get("likeCount") or r.get("Likes") or "",
            "License": r.get("License") or "",
            "Categories": r.get("Categories") or "",
            "Face Count": r.get("Face Count") or "",
            "Downloadable": r.get("Downloadable") or "",
            "Thumbnail": r.get("Thumbnail") or "",
            "Viewer URL": r.get("viewerUrl") or r.get("Viewer URL") or "",
            "Found At": now,
            "Query": query or r.get("Query") or "",
        })
    combined = _sanitize_df(pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True))
    liked = colls = pd.DataFrame()
    subs = pd.DataFrame(columns=SUBS_COLUMNS)
    if os.path.exists(XL_PATH):
        try:
            liked, colls, subs = read_workbook(scan_downloads=False)
        except Exception:
            pass
    if subs.empty:
        subs = pd.DataFrame(columns=SUBS_COLUMNS)
    write_workbook(
        liked if not liked.empty else pd.DataFrame(),
        colls if not colls.empty else pd.DataFrame(),
        subs,
        browse_df=combined,
    )
    return XL_PATH, len(new_rows)
