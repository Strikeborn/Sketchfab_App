"""Normalize Sketchfab collection search / detail payloads for UI tabs."""
from __future__ import annotations

from collection_urls import collection_public_url


def _thumb_from_collection(c: dict) -> str:
    thumbs = c.get("thumbnails") or {}
    images = thumbs.get("images") if isinstance(thumbs, dict) else None
    if not images:
        return ""
    by_w = sorted(images, key=lambda i: int(i.get("width") or 0))
    mid = by_w[len(by_w) // 2] if by_w else {}
    return str(mid.get("url") or by_w[-1].get("url") or "")


def _user_blob(c: dict) -> dict:
    user = c.get("user") or c.get("owner") or {}
    return user if isinstance(user, dict) else {}


def normalize_collection(c: dict) -> dict:
    """Flatten a collection search/detail/subscription result for cards."""
    if not isinstance(c, dict):
        return {}
    # Subscriptions sometimes nest the collection under "collection"
    nested = c.get("collection")
    if isinstance(nested, dict) and nested.get("uid"):
        c = {**nested, **{k: v for k, v in c.items() if k not in nested}}

    user = _user_blob(c)
    uid = str(c.get("uid") or "").strip()
    name = str(c.get("name") or "").strip()
    slug = str(c.get("slug") or "").strip()
    username = str(user.get("username") or "").strip()
    author = str(user.get("displayName") or username or "").strip()
    url = str(c.get("collectionUrl") or "").strip()
    if not url and uid:
        url = collection_public_url(name, uid, slug=slug, username=username or None)

    model_count = c.get("modelCount")
    try:
        model_count = int(model_count) if model_count is not None else None
    except (TypeError, ValueError):
        model_count = None

    sub_count = c.get("subscriberCount")
    if sub_count is None:
        sub_count = c.get("subscriptionCount")
    try:
        sub_count = int(sub_count) if sub_count is not None else None
    except (TypeError, ValueError):
        sub_count = None

    return {
        "UID": uid,
        "Name": name,
        "Slug": slug,
        "Author": author,
        "Author Username": username,
        "Author Profile": str(user.get("profileUrl") or "").strip(),
        "Model Count": model_count,
        "Subscriber Count": sub_count,
        "Like Count": int(user.get("likeCount") or 0) if user.get("likeCount") is not None else None,
        "URL": url,
        "Thumbnail": _thumb_from_collection(c),
        "Description": str(c.get("description") or "").strip(),
        "Updated At": str(c.get("updatedAt") or "").strip(),
        "Created At": str(c.get("createdAt") or "").strip(),
    }


def unwrap_collection_model(item: dict) -> dict:
    """Collection model list items are usually full models; sometimes nested."""
    if not isinstance(item, dict):
        return {}
    nested = item.get("model")
    if isinstance(nested, dict) and nested.get("uid"):
        return nested
    return item
