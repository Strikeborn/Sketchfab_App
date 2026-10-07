"""Normalize Sketchfab search API results for the browse tab."""
from __future__ import annotations


def thumb_urls_from_api(model: dict) -> tuple[str, str]:
    images = (model.get("thumbnails") or {}).get("images") or []
    if not images:
        return "", ""
    by_w = sorted(images, key=lambda i: int(i.get("width") or 0))
    low = str(by_w[0].get("url") or "")
    hi = str(by_w[-1].get("url") or low)
    if len(by_w) >= 2:
        target = min(512, int(by_w[-1].get("width") or 512))
        mid = min(by_w, key=lambda i: abs(int(i.get("width") or 0) - target))
        display = str(mid.get("url") or hi)
    else:
        display = hi
    return display, hi


def normalize_search_model(model: dict) -> dict:
    user = model.get("user") or {}
    lic = model.get("license") or {}
    if isinstance(lic, dict):
        lic_label = lic.get("label") or lic.get("slug") or ""
    else:
        lic_label = str(lic)
    cats = model.get("categories") or []
    cat_names = [c.get("name", "") for c in cats if isinstance(c, dict) and c.get("name")]
    low, hi = thumb_urls_from_api(model)
    faces = model.get("faceCount")
    username = str(user.get("username") or "").strip()
    return {
        "UID": model.get("uid") or "",
        "Name": model.get("name") or "",
        "Author": user.get("displayName") or username or "",
        "Author Username": username,
        "Thumbnail": low,
        "Thumbnail HD": hi,
        "viewCount": int(model.get("viewCount") or 0),
        "likeCount": int(model.get("likeCount") or 0),
        "downloadCount": int(model.get("downloadCount") or 0) if model.get("downloadCount") is not None else None,
        "License": lic_label,
        "Face Count": f"{int(faces):,}" if faces is not None else "",
        "Downloadable": "Yes" if model.get("isDownloadable") else "No",
        "Categories": ", ".join(cat_names),
        "viewerUrl": model.get("viewerUrl") or "",
        "publishedAt": model.get("publishedAt") or "",
    }
