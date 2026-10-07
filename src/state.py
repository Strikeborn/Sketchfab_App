from __future__ import annotations
from dataclasses import dataclass, field
import os
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

UID_ALIASES = ("UID", "Model UID")
NAME_ALIASES = ("Name", "Model Name")
ASSIGNED_ALIASES = ("Assigned Collection(s)", "Assigned Collection")


def col(df: pd.DataFrame, aliases: tuple[str, ...], default: str = "") -> str:
    for a in aliases:
        if a in df.columns:
            return a
    return default


def uid_col(df: pd.DataFrame) -> str:
    return col(df, UID_ALIASES)


def name_col(df: pd.DataFrame) -> str:
    return col(df, NAME_ALIASES)


def _coerce_str_series(s: pd.Series) -> pd.Series:
    return s.astype(object).where(pd.notna(s), "").astype(str).replace({"nan": "", "None": "", "<NA>": ""})


def normalize_liked_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    if "Assigned Collection" in out.columns and "Assigned Collection(s)" not in out.columns:
        out["Assigned Collection(s)"] = out["Assigned Collection"]
    if "Fuzzy Matched Collection(s)" in out.columns and "Fuzzy Match Collection(s)" not in out.columns:
        out["Fuzzy Match Collection(s)"] = out["Fuzzy Matched Collection(s)"]
    for c in ("Manual", "Push Sent", "Pushed At", "Assigned At", "Collected At", "Liked Order", "Liked At", "Author Username", "Downloaded", "Download Path", "Thumbnail", "Thumbnail HD", "License", "Face Count", "Views", "Likes", "Downloads", "Assignment Notes", "Categories", "Original Source", "Original Format", "Original Size"):
        if c not in out.columns:
            out[c] = ""
        else:
            out[c] = _coerce_str_series(out[c])
    for c in ("Assigned Collection(s)", "Already In Collection(s)", "Auto-Assigned Collection(s)", "Suggested Collection(s)", "Fuzzy Match Collection(s)", "Tags"):
        if c in out.columns:
            out[c] = _coerce_str_series(out[c])
    if "Downloadable" not in out.columns and "Is Downloadable" in out.columns:
        out["Downloadable"] = out["Is Downloadable"].apply(
            lambda v: "Yes" if str(v).strip().lower() in {"1", "true", "yes"} else "No"
        )
    if "Liked Order" in out.columns:
        out["Liked Order"] = pd.to_numeric(out["Liked Order"], errors="coerce")
    return out


MODEL_EXTENSIONS = {".glb", ".gltf", ".fbx", ".obj", ".blend", ".zip", ".usdz", ".dae", ".stl"}
UID_RE = re.compile(r"[a-f0-9]{32}", re.IGNORECASE)
UID_SUFFIX_RE = re.compile(r"[_-]([a-f0-9]{8,32})(?:\.[a-z0-9]+)?$", re.IGNORECASE)


def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    return here.parent if here.name == "src" else here


DOWNLOAD_FOLDER_NAME = "Sketchfab_Downloads"


def _is_drive_root(p: Path) -> bool:
    s = str(p).strip().rstrip("\\/")
    return len(s) <= 3 and ":" in s


def resolve_download_paths() -> list[Path]:
    """Resolve full paths to …/Sketchfab_Downloads from env."""
    name = DOWNLOAD_FOLDER_NAME
    seen: set[str] = set()
    out: list[Path] = []

    def _add(path: Path) -> None:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)

    root = os.environ.get("SKETCHFAB_DOWNLOAD_ROOT", "").strip()
    if root:
        _add(Path(root) / name)

    raw = os.environ.get("SKETCHFAB_DOWNLOAD_DIRS", "").strip()
    for part in [p.strip() for p in raw.split(";") if p.strip()]:
        p = Path(part)
        if p.name.lower() == name.lower():
            _add(p)
        elif _is_drive_root(p):
            _add(p / name)
        elif p.drive:
            # Legacy e.g. F:\Sketchfab → F:\Sketchfab_Downloads
            _add(Path(p.drive + "\\") / name)
        else:
            _add(p / name)

    if not out:
        for base in (Path("F:/"), Path.home()):
            _add(base / name)

    return out


def ensure_download_dirs() -> list[Path]:
    """Create Sketchfab_Downloads under configured root(s) if missing."""
    ready: list[Path] = []
    for path in resolve_download_paths():
        try:
            path.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                ready.append(path)
        except OSError:
            continue
    return ready


def get_configured_download_dirs() -> list[Path]:
    return resolve_download_paths()


def get_download_dirs() -> list[Path]:
    """Existing download folders (creates Sketchfab_Downloads if configured)."""
    return ensure_download_dirs()


def download_dirs_status() -> str:
    paths = resolve_download_paths()
    ready = [p for p in paths if p.exists() and p.is_dir()]
    if ready:
        return str(ready[0]) if len(ready) == 1 else f"{len(ready)} dirs"
    if paths:
        return f"{paths[0]} (could not create)"
    return "none — set SKETCHFAB_DOWNLOAD_ROOT in .env (e.g. F:\\)"


def build_uid_index(search_dirs: list[Path] | None = None) -> dict[str, str]:
    index: dict[str, str] = {}
    dirs = search_dirs or get_download_dirs()
    for root in dirs:
        try:
            for dirpath, _, filenames in os.walk(root):
                folder = Path(dirpath)
                # Per-model sketchfab.txt (UID without putting it in the .glb name)
                meta = folder / "sketchfab.txt"
                meta_uid = ""
                if meta.is_file():
                    try:
                        for line in meta.read_text(encoding="utf-8", errors="ignore").splitlines():
                            if line.lower().startswith("uid:"):
                                meta_uid = line.split(":", 1)[1].strip().lower()
                                break
                    except OSError:
                        meta_uid = ""
                if meta_uid and meta_uid not in index:
                    # Point at folder until we find a model file below
                    index[meta_uid] = dirpath

                for m in UID_RE.finditer(dirpath):
                    uid = m.group(0).lower()
                    if uid not in index:
                        index[uid] = dirpath
                for fname in filenames:
                    ext = Path(fname).suffix.lower()
                    if ext not in MODEL_EXTENSIONS:
                        continue
                    if fname.casefold() == "sketchfab.txt":
                        continue
                    full = os.path.join(dirpath, fname)
                    matched_uids: set[str] = set()
                    for m in UID_RE.finditer(fname):
                        matched_uids.add(m.group(0).lower())
                    sm = UID_SUFFIX_RE.search(fname)
                    if sm:
                        matched_uids.add(sm.group(1).lower())
                    if meta_uid:
                        matched_uids.add(meta_uid)
                    for uid in matched_uids:
                        # Prefer file path over bare folder
                        prev = index.get(uid, "")
                        if not prev or os.path.isdir(prev):
                            index[uid] = full
                        elif uid not in index:
                            index[uid] = full
        except OSError:
            continue
    return index


def enrich_download_status(df: pd.DataFrame, search_dirs: list[Path] | None = None) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    uid_col_name = "UID" if "UID" in out.columns else ("Model UID" if "Model UID" in out.columns else None)
    if not uid_col_name:
        return out
    dirs = search_dirs or get_download_dirs()
    index = build_uid_index(dirs)
    downloaded, paths = [], []
    for uid in out[uid_col_name].astype(str):
        key = uid.strip().lower()
        path = index.get(key, "")
        if not path and len(key) == 32:
            path = index.get(key[:8], "")
        if not path and len(key) == 32:
            short = key[:8]
            for root in dirs:
                try:
                    hits = list(root.rglob(f"*_{short}.glb")) + list(root.rglob(f"*_{short}.gltf"))
                    if hits:
                        path = str(hits[0])
                        break
                except OSError:
                    continue
        downloaded.append("Yes" if path else "No")
        paths.append(path)
    out["Downloaded"] = downloaded
    out["Download Path"] = paths
    return out


def mark_downloaded_inplace(df: pd.DataFrame, uid: str, path: str | Path) -> bool:
    """Set Downloaded/Download Path for one UID without scanning disk. Returns True if row found."""
    if df is None or df.empty or not uid:
        return False
    uid_col_name = "UID" if "UID" in df.columns else ("Model UID" if "Model UID" in df.columns else None)
    if not uid_col_name:
        return False
    want = str(uid).strip().lower()
    mask = df[uid_col_name].astype(str).str.strip().str.lower() == want
    if not mask.any() and len(want) == 32:
        mask = df[uid_col_name].astype(str).str.strip().str.lower().str.startswith(want[:8])
    if not mask.any():
        return False
    if "Downloaded" not in df.columns:
        df["Downloaded"] = ""
    if "Download Path" not in df.columns:
        df["Download Path"] = ""
    df.loc[mask, "Downloaded"] = "Yes"
    df.loc[mask, "Download Path"] = str(path or "")
    return True


@dataclass
class AppState:
    liked_df: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    colls_df: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    subs_df: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    overwrite: bool = False
    dry_run: bool = True
    busy: bool = False
    username: str = ""
    # paging
    liked_page: int = 0
    colls_page: int = 0
    subs_page: int = 0
    liked_page_size: int = 50
    colls_page_size: int = 100
    subs_page_size: int = 50
    search_page: int = 0
    search_page_size: int = 48
    hide_nsfw: bool = True
    hide_female: bool = True
    hide_male: bool = True