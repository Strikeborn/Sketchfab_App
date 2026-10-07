from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable

import requests

from sketchfab_client import SketchfabClient, humanize_sketchfab_error

# (bytes_done, bytes_total, speed_instant_bps, speed_avg_bps)
ProgressFn = Callable[[int, int, float, float], None]

_SAFE_RE = re.compile(r"[^\w\-. ]+", re.UNICODE)
_FOLDER_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
UNLISTED_FOLDER = "Unlisted"
UNSORTED_FOLDER = "_Unsorted"
META_FILENAME = "sketchfab.txt"


def _safe_segment(text: str, *, max_len: int = 80) -> str:
    raw = (text or "").strip()
    cleaned = _FOLDER_RE.sub("_", raw).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:max_len].rstrip(" .") if cleaned else "")


def safe_collection_folder(name: str) -> str:
    """Windows-safe folder name for a Sketchfab collection."""
    return _safe_segment(name) or UNSORTED_FOLDER


def safe_model_stem(name: str) -> str:
    """Filename/folder stem from model title — no UID."""
    return _safe_segment(name, max_len=60) or "model"


def safe_model_folder(name: str, author: str = "", license: str = "") -> str:
    """
    Per-model folder: \"Title - Author - License\" (omit empty parts).
    No long Sketchfab UID in the folder name.
    """
    parts = [safe_model_stem(name)]
    a = _safe_segment(author, max_len=40)
    lic = _safe_segment(license, max_len=40)
    if a:
        parts.append(a)
    if lic:
        parts.append(lic)
    folder = " - ".join(parts)
    return folder[:120].rstrip(" .-") or "model"


def collections_for_download(
    already_in: object = "",
    assigned: object = "",
) -> list[str]:
    """
    Ordered collection folders for a download.
    Prefer Already In (live membership), then Assigned; skip Unlisted / empties.
    """
    out: list[str] = []
    seen: set[str] = set()
    for cell in (already_in, assigned):
        raw = "" if cell is None else str(cell)
        if raw.strip().casefold() in {"", "nan", "none", "<na>"}:
            continue
        for part in raw.split(","):
            name = part.strip()
            if not name:
                continue
            key = name.casefold()
            if key == "unlisted" or key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out


def write_model_meta(
    folder: Path,
    *,
    uid: str,
    name: str = "",
    author: str = "",
    license: str = "",
    url: str = "",
) -> Path:
    """Write sketchfab.txt so Scan/Organize can find the UID without it in the filename."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    uid = (uid or "").strip().lower()
    lines = [
        f"Name: {(name or '').strip()}",
        f"Author: {(author or '').strip()}",
        f"License: {(license or '').strip()}",
        f"UID: {uid}",
    ]
    if url:
        lines.append(f"URL: {url.strip()}")
    elif uid and len(uid) == 32:
        lines.append(f"URL: https://skfb.ly/{uid}")  # may 404; still useful hint
        lines.append(f"Model: https://sketchfab.com/3d-models/{uid}")
    path = folder / META_FILENAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_uid_from_meta(folder: Path) -> str:
    path = Path(folder) / META_FILENAME
    if not path.is_file():
        return ""
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lower().startswith("uid:"):
                return line.split(":", 1)[1].strip().lower()
    except OSError:
        pass
    return ""


def read_meta_field(folder: Path, key: str) -> str:
    path = Path(folder) / META_FILENAME
    if not path.is_file():
        return ""
    want = key.casefold() + ":"
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.casefold().startswith(want):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def resolve_model_dir(
    collection_dir: Path,
    *,
    name: str,
    author: str = "",
    license: str = "",
    uid: str = "",
) -> Path:
    """Pick {collection}/{Title - Author - License}/, disambiguate with short uid if needed."""
    collection_dir = Path(collection_dir)
    base = safe_model_folder(name, author, license)
    candidate = collection_dir / base
    uid = (uid or "").strip().lower()
    if not candidate.exists():
        return candidate
    existing = read_uid_from_meta(candidate)
    if uid and existing == uid:
        return candidate
    if uid and not existing:
        # Folder exists without meta — assume reuse if empty of foreign uids
        return candidate
    short = uid[:8] if len(uid) >= 8 else (uid or "dup")
    return collection_dir / f"{base} ({short})"


def model_glb_path(model_dir: Path, name: str) -> Path:
    return Path(model_dir) / f"{safe_model_stem(name)}.glb"


def _link_to_real(real: Path, link_path: Path) -> str:
    """Prefer hardlink, then symlink. Returns 'hardlink' | 'symlink' | 'exists' | 'skipped'."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if real.resolve() == link_path.resolve():
            return "exists"
    except OSError:
        pass
    if link_path.exists() or link_path.is_symlink():
        try:
            if _same_file(real, link_path):
                return "exists"
        except OSError:
            pass
        return "exists"
    try:
        os.link(real, link_path)
        return "hardlink"
    except OSError:
        pass
    try:
        os.symlink(real, link_path)
        return "symlink"
    except OSError:
        return "skipped"


def mirror_into_collections(real: Path, link_dirs: list[Path]) -> list[tuple[Path, str]]:
    """Hardlink/symlink ``real`` into each model folder (same filename)."""
    results: list[tuple[Path, str]] = []
    if not real or not real.is_file():
        return results
    for d in link_dirs or []:
        dest_dir = Path(d)
        if not dest_dir:
            continue
        link = dest_dir / real.name
        kind = _link_to_real(real, link)
        results.append((link, kind))
    return results


def _same_file(a: Path, b: Path) -> bool:
    try:
        if not a.is_file() or not b.is_file():
            return False
        sa, sb = a.stat(), b.stat()
        if sa.st_ino and sb.st_ino and sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino:
            return True
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _uid_from_filename(fname: str) -> str:
    from state import UID_RE, UID_SUFFIX_RE

    full = UID_RE.findall(fname)
    if full:
        for m in full:
            if len(m) == 32:
                return m.lower()
        return full[-1].lower()
    sm = UID_SUFFIX_RE.search(fname)
    if sm:
        return sm.group(1).lower()
    return ""


def _uid_for_model_file(path: Path) -> str:
    """UID from filename, else sketchfab.txt in the same folder."""
    uid = _uid_from_filename(path.name)
    if uid:
        return uid
    return read_uid_from_meta(path.parent)


def _root_containing(path: Path, roots: list[Path]) -> Path | None:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return root
        except ValueError:
            continue
    return None


def find_download_files(roots: list[Path]) -> dict[str, list[Path]]:
    """Map uid → all model file paths under download roots."""
    from state import MODEL_EXTENSIONS

    by_uid: dict[str, list[Path]] = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        try:
            for dirpath, _, filenames in os.walk(root):
                folder = Path(dirpath)
                for fname in filenames:
                    if Path(fname).suffix.lower() not in MODEL_EXTENSIONS:
                        continue
                    if fname.casefold() == META_FILENAME:
                        continue
                    full = folder / fname
                    uid = _uid_for_model_file(full)
                    if not uid:
                        continue
                    by_uid.setdefault(uid, []).append(full)
                    if len(uid) == 32:
                        by_uid.setdefault(uid[:8], []).append(full)
        except OSError:
            continue
    return by_uid


def _row_fields(row) -> tuple[str, str, str, str, str]:
    """name, author, license, already, assigned from a workbook row."""
    if row is None:
        return "", "", "", "", ""
    get = row.get if hasattr(row, "get") else lambda k, d="": d
    name = str(get("Name") or get("Model Name") or "").strip()
    author = str(get("Author") or "").strip()
    if author.casefold() in {"nan", "none", "<na>"}:
        author = ""
    license_ = str(get("License") or "").strip()
    if license_.casefold() in {"nan", "none", "<na>"}:
        license_ = ""
    already = get("Already In Collection(s)") or ""
    assigned = get("Assigned Collection(s)") or ""
    return name, author, license_, already, assigned


def organize_downloads_by_collection(
    liked_df,
    roots: list[Path] | None = None,
) -> dict:
    """
    Layout: {Collection}/{Title - Author - License}/{Title}.glb + sketchfab.txt

    Only creates folders for collections/models that already have files on disk.
    Extra collections get a hardlinked .glb in their own model folder.
    """
    from state import get_download_dirs, uid_col

    roots = [Path(r) for r in (roots or get_download_dirs())]
    stats = {
        "moved": 0,
        "linked": 0,
        "already_ok": 0,
        "unsorted": 0,
        "folders": set(),
        "files": 0,
        "errors": [],
    }
    if not roots:
        stats["errors"].append("No download roots")
        return stats

    by_uid = find_download_files(roots)
    uids = sorted({u for u in by_uid if len(u) == 32} or set(by_uid.keys()))
    row_by_uid: dict[str, object] = {}
    if liked_df is not None and not getattr(liked_df, "empty", True):
        uc = uid_col(liked_df)
        if uc:
            for _, row in liked_df.iterrows():
                key = str(row.get(uc) or "").strip().lower()
                if key:
                    row_by_uid[key] = row
                    if len(key) == 32:
                        row_by_uid.setdefault(key[:8], row)

    for uid in uids:
        paths = list(by_uid.get(uid) or [])
        if len(uid) == 32:
            for p in by_uid.get(uid[:8]) or []:
                if p not in paths:
                    paths.append(p)
        uniq: list[Path] = []
        seen_res: set[str] = set()
        for p in paths:
            try:
                key = str(p.resolve())
            except OSError:
                key = str(p)
            if key in seen_res:
                continue
            seen_res.add(key)
            uniq.append(p)
        paths = [p for p in uniq if p.is_file()]
        if not paths:
            continue
        stats["files"] += 1

        row = row_by_uid.get(uid)
        if row is None and len(uid) >= 8:
            row = row_by_uid.get(uid[:8])
        name, author, license_, already, assigned = _row_fields(row)
        if not name:
            # Fall back to filename stem without trailing _uid
            stem = paths[0].stem
            name = re.sub(r"[_-][a-f0-9]{8,32}$", "", stem, flags=re.I).strip(" _-") or stem
        if not author:
            author = read_meta_field(paths[0].parent, "Author")
        if not license_:
            license_ = read_meta_field(paths[0].parent, "License")

        colls = collections_for_download(already, assigned)
        if not colls:
            colls = [UNSORTED_FOLDER]
            stats["unsorted"] += 1

        root = _root_containing(paths[0], roots) or roots[0]
        primary_coll = root / safe_collection_folder(colls[0])
        model_dir = resolve_model_dir(
            primary_coll, name=name, author=author, license=license_, uid=uid
        )
        target = model_glb_path(model_dir, name)

        # Prefer a source already in the target model folder
        src = paths[0]
        for p in paths:
            try:
                if p.parent.resolve() == model_dir.resolve():
                    src = p
                    break
            except OSError:
                continue

        try:
            model_dir.mkdir(parents=True, exist_ok=True)
            stats["folders"].add(str(model_dir))

            if target.exists() and (_same_file(src, target) or src.resolve() == target.resolve()):
                real = target
                stats["already_ok"] += 1
            elif target.exists() and not _same_file(src, target):
                real = target
                stats["already_ok"] += 1
            else:
                if src.resolve() != target.resolve():
                    # Move/rename into model folder (drops long uid from filename)
                    src.replace(target)
                    stats["moved"] += 1
                real = target

            write_model_meta(
                model_dir, uid=uid, name=name, author=author, license=license_
            )

            # Other collections: same model-folder layout + hardlinked glb
            link_model_dirs: list[Path] = []
            for c in colls[1:]:
                other = resolve_model_dir(
                    root / safe_collection_folder(c),
                    name=name,
                    author=author,
                    license=license_,
                    uid=uid,
                )
                other.mkdir(parents=True, exist_ok=True)
                write_model_meta(
                    other, uid=uid, name=name, author=author, license=license_
                )
                link_model_dirs.append(other)
                stats["folders"].add(str(other))

            for _, kind in mirror_into_collections(real, link_model_dirs):
                if kind in {"hardlink", "symlink"}:
                    stats["linked"] += 1

            wanted_dirs = {model_dir.resolve()}
            for d in link_model_dirs:
                wanted_dirs.add(d.resolve())

            for p in paths:
                try:
                    if _same_file(p, real) or p.resolve() == real.resolve():
                        if p.parent.resolve() not in wanted_dirs and p.resolve() != real.resolve():
                            p.unlink(missing_ok=True)
                        continue
                    if p.parent.resolve() not in wanted_dirs:
                        p.unlink(missing_ok=True)
                except OSError:
                    continue
        except Exception as exc:
            stats["errors"].append(f"{uid}: {exc}")

    stats["folders"] = sorted(stats["folders"])
    return stats


def download_glb(
    uid: str,
    name: str,
    dest_dir: Path,
    client: SketchfabClient | None = None,
    *,
    link_dirs: list[Path] | None = None,
    author: str = "",
    license: str = "",
    on_progress: ProgressFn | None = None,
) -> Path:
    """
    Download GLB into a per-model folder.

    ``dest_dir`` should be the model folder
    ``{Collection}/{Title - Author - License}/``.
    Writes sketchfab.txt (UID/author/license) beside the GLB; optional hardlinks
    into other model folders via ``link_dirs``.
    ``on_progress(bytes_done, bytes_total, speed_now_bps, speed_avg_bps)`` is
    called ~4×/s while the file streams (bytes_total may be 0 if unknown).
    """
    if not uid:
        raise ValueError("Model UID required")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    client = client or SketchfabClient.for_download()
    if on_progress:
        try:
            on_progress(0, 0, 0.0, 0.0)
        except Exception:
            pass
    meta = client.get_model_download(uid)
    url = ""
    picked: dict = {}
    for key in ("glb", "gltf", "source", "usdz"):
        block = meta.get(key) if isinstance(meta.get(key), dict) else {}
        cand = (block or {}).get("url")
        if cand:
            url = cand
            picked = block
            break
    if not url:
        raise RuntimeError("No download URL returned (glb/gltf/source/usdz) — model may not be downloadable.")
    out = model_glb_path(dest_dir, name)
    try:
        resp = requests.get(url, timeout=(30, 300), stream=True)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        code = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
        if code == 403:
            raise RuntimeError(
                "CDN download forbidden (403) — signed URL expired or blocked; retry in a minute."
            ) from exc
        raise RuntimeError(humanize_sketchfab_error(exc)) from exc
    try:
        total = int(resp.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        total = 0
    # Sketchfab download meta sometimes includes size when CDN omits Content-Length.
    if total <= 0:
        for key in ("size", "contentLength", "content_length"):
            raw = picked.get(key) if isinstance(picked, dict) else None
            if raw is None and isinstance(meta.get("gltf"), dict):
                raw = meta["gltf"].get(key)
            try:
                total = int(raw or 0)
            except (TypeError, ValueError):
                total = 0
            if total > 0:
                break
    done = 0
    t0 = time.monotonic()
    last_t = t0
    last_done = 0
    with open(out, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            f.write(chunk)
            done += len(chunk)
            if not on_progress:
                continue
            now = time.monotonic()
            if now - last_t < 0.25 and (total <= 0 or done < total):
                continue
            dt = max(1e-6, now - last_t)
            elapsed = max(1e-6, now - t0)
            speed_now = (done - last_done) / dt
            speed_avg = done / elapsed
            last_t = now
            last_done = done
            try:
                on_progress(done, total, speed_now, speed_avg)
            except Exception:
                pass
    if on_progress:
        elapsed = max(1e-6, time.monotonic() - t0)
        try:
            on_progress(done, total or done, done / elapsed, done / elapsed)
        except Exception:
            pass
    write_model_meta(
        dest_dir, uid=uid, name=name, author=author, license=license
    )
    for link_dir in link_dirs or []:
        ld = Path(link_dir)
        ld.mkdir(parents=True, exist_ok=True)
        write_model_meta(ld, uid=uid, name=name, author=author, license=license)
    if link_dirs:
        mirror_into_collections(out, list(link_dirs))
    return out
