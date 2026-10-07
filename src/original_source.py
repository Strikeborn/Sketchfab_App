"""Detect Sketchfab 'original source' archives (FBX/OBJ/DAE/…) via Download API."""
from __future__ import annotations

import io
import struct
import zipfile
from collections import Counter
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

# Prefer mesh-ish originals; zip often wraps the real file.
_MESH_EXTS = {
    ".fbx", ".obj", ".dae", ".blend", ".max", ".c4d", ".ma", ".mb",
    ".3ds", ".stl", ".ply", ".abc", ".usd", ".usda", ".usdc", ".gltf", ".glb",
}
_SKIP_EXTS = {".jpeg", ".jpg", ".png", ".webp", ".gif", ".bmp", ".tga", ".tif", ".tiff", ".bin", ".txt", ".md"}

ORIGINAL_COL = "Original Source"  # Yes / No / blank
ORIGINAL_FMT_COL = "Original Format"  # fbx, dae, … or mixed / zip / unknown
ORIGINAL_SIZE_COL = "Original Size"  # human or bytes as string

# List zip central directory without downloading whole archive (Range).
_EOCD_SCAN = 128 * 1024


def _yes_no(flag: bool | None) -> str:
    if flag is True:
        return "Yes"
    if flag is False:
        return "No"
    return ""


def _fmt_size(n: int | None) -> str:
    if not n or n < 0:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def list_remote_zip_names(url: str, *, timeout: int = 60) -> list[str]:
    """
    Return entry names from a remote ZIP using HTTP Range on the EOCD/central dir.
    Falls back to empty list on failure.
    """
    url = (url or "").strip()
    if not url:
        return []
    try:
        # Tail scan for End of Central Directory
        headers = {"Range": f"bytes=-{_EOCD_SCAN}"}
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code not in (200, 206):
            return []
        data = r.content
        sig = b"PK\x05\x06"
        pos = data.rfind(sig)
        if pos < 0:
            return []
        # EOCD: disk nums (4) + entries (4) + cd_size (4) + cd_offset (4) + comment_len (2)
        if pos + 22 > len(data):
            return []
        cd_size, cd_offset, comment_len = struct.unpack_from("<IIH", data, pos + 12)
        if comment_len < 0 or cd_size <= 0 or cd_size > 50_000_000:
            return []
        # Absolute EOCD start may be after range start when 206
        # Prefer fetching central directory by absolute offset
        end = cd_offset + cd_size - 1
        r2 = requests.get(
            url,
            headers={"Range": f"bytes={cd_offset}-{end}"},
            timeout=timeout,
        )
        if r2.status_code not in (200, 206):
            return []
        cd = r2.content
        names: list[str] = []
        i = 0
        while i + 46 <= len(cd):
            if cd[i : i + 4] != b"PK\x01\x02":
                break
            name_len, extra_len, comment_len2 = struct.unpack_from("<HHH", cd, i + 28)
            i_name = i + 46
            i_next = i_name + name_len + extra_len + comment_len2
            if i_next > len(cd):
                break
            try:
                names.append(cd[i_name : i_name + name_len].decode("utf-8", errors="replace"))
            except Exception:
                pass
            i = i_next
        return names
    except Exception:
        return []


def formats_from_names(names: list[str]) -> list[str]:
    """Rank mesh extensions found in zip entry paths (ignore pure textures)."""
    counts: Counter[str] = Counter()
    for raw in names:
        n = (raw or "").replace("\\", "/").strip()
        if not n or n.endswith("/"):
            continue
        # Prefer innermost path parts; skip obvious texture folders only by extension.
        ext = Path(n).suffix.lower()
        if not ext or ext in _SKIP_EXTS:
            continue
        if ext == ".zip":
            counts[".zip"] += 1
            continue
        if ext in _MESH_EXTS:
            counts[ext] += 1
        else:
            # keep rare oddballs lightly
            if ext not in {".json", ".xml", ".mtl"}:
                counts[ext] += 1
    if not counts:
        return []
    # Prefer real mesh types over wrapping .zip
    mesh = [e for e, _ in counts.most_common() if e in _MESH_EXTS]
    if mesh:
        return [e.lstrip(".") for e in mesh]
    return [e.lstrip(".") for e, _ in counts.most_common(4)]


def summarize_formats(fmts: list[str]) -> str:
    if not fmts:
        return "unknown"
    # unique preserve order
    seen: list[str] = []
    for f in fmts:
        if f and f not in seen:
            seen.append(f)
    if len(seen) == 1:
        return seen[0]
    if len(seen) <= 3:
        return "+".join(seen)
    return "mixed"


def probe_original(
    client,
    uid: str,
    *,
    peek_names: bool = True,
) -> dict:
    """
    Returns:
      has_source: bool
      size: int | None
      formats: list[str]   # e.g. ['fbx'] or ['dae','jpg'] filtered
      format_label: str    # fbx / dae / zip / unknown / ''
      names_sample: list[str]
      error: str
    """
    uid = (uid or "").strip()
    out = {
        "has_source": False,
        "size": None,
        "formats": [],
        "format_label": "",
        "names_sample": [],
        "error": "",
    }
    if not uid:
        out["error"] = "no uid"
        return out
    try:
        meta = client.get_model_download(uid)
    except Exception as e:
        out["error"] = str(e)
        return out

    src = meta.get("source") if isinstance(meta, dict) else None
    if not isinstance(src, dict) or not src.get("url"):
        out["has_source"] = False
        out["format_label"] = ""
        return out

    out["has_source"] = True
    try:
        out["size"] = int(src.get("size") or 0) or None
    except (TypeError, ValueError):
        out["size"] = None

    if not peek_names:
        out["format_label"] = "zip"
        return out

    names = list_remote_zip_names(str(src["url"]))
    out["names_sample"] = names[:30]
    nested_extra: list[str] = []

    def _mostly_wrapper(ns: list[str]) -> bool:
        if not ns:
            return True
        meshish = 0
        for n in ns:
            if n.endswith("/"):
                continue
            ext = Path(n).suffix.lower()
            if ext in _MESH_EXTS:
                meshish += 1
        return meshish == 0

    # Small archives: download + open nested source/*.zip for real mesh types.
    if (not names or _mostly_wrapper(names)) and out["size"] and out["size"] <= 12_000_000:
        try:
            raw = requests.get(str(src["url"]), timeout=120).content
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = zf.namelist()
                out["names_sample"] = names[:30]
                for n in names:
                    low = n.replace("\\", "/").lower()
                    if not (low.startswith("source/") and low.endswith(".zip")):
                        continue
                    try:
                        with zipfile.ZipFile(io.BytesIO(zf.read(n))) as z2:
                            nested_extra.extend(z2.namelist())
                    except Exception:
                        pass
        except Exception:
            pass

    fmts = formats_from_names(list(names) + nested_extra)
    # Heuristic: nested archive named like model.fbx.zip won't show fbx ext at outer level.
    for n in names:
        base = Path(n.replace("\\", "/")).name.lower()
        for mesh in _MESH_EXTS:
            token = mesh.lstrip(".")
            if f".{token}." in base or base.endswith(f".{token}.zip"):
                if token not in fmts:
                    fmts.insert(0, token)

    out["formats"] = fmts
    out["format_label"] = summarize_formats(fmts) if fmts else ("zip" if names else "unknown")
    return out


def enrich_original_sources(
    liked_df: pd.DataFrame,
    client,
    *,
    only_missing: bool = True,
    peek_names: bool = True,
    downloadable_only: bool = True,
    limit: int | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> pd.DataFrame:
    """Fill Original Source / Format / Size columns for liked rows."""
    if liked_df is None or liked_df.empty:
        return liked_df
    out = liked_df.copy()
    for c in (ORIGINAL_COL, ORIGINAL_FMT_COL, ORIGINAL_SIZE_COL):
        if c not in out.columns:
            out[c] = ""
        else:
            out[c] = out[c].fillna("").astype(str)

    uid_c = "UID" if "UID" in out.columns else ("Model UID" if "Model UID" in out.columns else "")
    if not uid_c:
        return out

    idxs: list[int] = []
    for i, row in out.iterrows():
        uid = str(row.get(uid_c) or "").strip()
        if not uid:
            continue
        if downloadable_only:
            dl = str(row.get("Downloadable") or "").strip().lower()
            if dl not in {"yes", "y", "true", "1"}:
                # Still record No so we don't re-hit forever
                if only_missing and str(row.get(ORIGINAL_COL) or "").strip():
                    continue
                out.at[i, ORIGINAL_COL] = "No"
                out.at[i, ORIGINAL_FMT_COL] = ""
                out.at[i, ORIGINAL_SIZE_COL] = ""
                continue
        if only_missing and str(row.get(ORIGINAL_COL) or "").strip():
            continue
        idxs.append(i)

    if limit is not None:
        idxs = idxs[: max(0, int(limit))]

    total = len(idxs)
    for n, i in enumerate(idxs, start=1):
        uid = str(out.at[i, uid_c] or "").strip()
        if on_progress:
            on_progress(n, total, uid)
        info = probe_original(client, uid, peek_names=peek_names)
        if info.get("error") and not info.get("has_source"):
            # leave blank on hard failure so retry can run later
            if "404" in info["error"] or "403" in info["error"] or "Unavailable" in info["error"]:
                out.at[i, ORIGINAL_COL] = "No"
                out.at[i, ORIGINAL_FMT_COL] = ""
                out.at[i, ORIGINAL_SIZE_COL] = ""
            continue
        out.at[i, ORIGINAL_COL] = _yes_no(bool(info.get("has_source")))
        if info.get("has_source"):
            out.at[i, ORIGINAL_FMT_COL] = info.get("format_label") or "unknown"
            out.at[i, ORIGINAL_SIZE_COL] = _fmt_size(info.get("size"))
        else:
            out.at[i, ORIGINAL_FMT_COL] = ""
            out.at[i, ORIGINAL_SIZE_COL] = ""
    return out
