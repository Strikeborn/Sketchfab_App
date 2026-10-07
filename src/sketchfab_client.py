from __future__ import annotations
import os
import time
import typing as t
import logging
import threading
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

API_BASE = os.environ.get("SKETCHFAB_API_BASE", "https://api.sketchfab.com/v3")
TOKEN = os.environ.get("SKETCHFAB_TOKEN")

MIN_POST_INTERVAL_SEC = float(os.environ.get("MIN_POST_INTERVAL_SEC", "1.0"))
MIN_GET_INTERVAL_SEC = float(os.environ.get("MIN_GET_INTERVAL_SEC", "0.35"))
MIN_GET_INTERVAL_DOWNLOAD_SEC = float(os.environ.get("MIN_GET_INTERVAL_DOWNLOAD_SEC", "0.12"))
MAX_REQUEST_RETRIES = int(os.environ.get("SKETCHFAB_MAX_RETRIES", "12"))
# Interactive creates shouldn't sit in 12×90s loops.
MAX_CREATE_RETRIES = int(os.environ.get("SKETCHFAB_CREATE_RETRIES", "4"))
PUSH_BATCH_SIZE = int(os.environ.get("PUSH_BATCH_SIZE", "12"))
# Extra idle between Push batches — stay under Sketchfab POST limits (avoid 429 snowball).
PUSH_BATCH_GAP_SEC = float(os.environ.get("PUSH_BATCH_GAP_SEC", "0.6"))
# Push uses a slower fixed lane than interactive creates (MIN_POST_INTERVAL_SEC).
PUSH_MIN_POST_INTERVAL_SEC = float(os.environ.get("PUSH_MIN_POST_INTERVAL_SEC", "2.2"))
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PRELOAD_WORKERS = int(os.environ.get("PUSH_PRELOAD_WORKERS", "4"))
# Real Push / Dry-run trust workbook Already In by default (much faster).
# Check sync passes verify_membership=True to list collection membership live.
# Set PUSH_VERIFY_MEMBERSHIP=1 to always re-check before posting.
PUSH_VERIFY_MEMBERSHIP = os.environ.get("PUSH_VERIFY_MEMBERSHIP", "").strip().lower() in {
    "1", "true", "yes", "on",
}
# After each POST, re-list the whole collection to confirm UIDs landed.
# Off by default — that crawl dominates Push time on large collections.
# Age-restricted ghosts are uncommon; use Check sync or set PUSH_VERIFY_AFTER=1 if needed.
PUSH_VERIFY_AFTER = os.environ.get("PUSH_VERIFY_AFTER", "").strip().lower() in {
    "1", "true", "yes", "on",
}


class RateLimitedError(RuntimeError):
    """Sketchfab 429 after retries exhausted — caller should back off / assign locally."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SketchfabApiError(RuntimeError):
    """HTTP failure with a user-facing hint (WAF, forbidden, etc.)."""

    def __init__(self, message: str, *, status: int | None = None, hint: str = ""):
        super().__init__(message)
        self.status = status
        self.hint = hint or message


def humanize_sketchfab_error(exc: BaseException) -> str:
    """Short Activity-log message for download / API failures."""
    if isinstance(exc, SketchfabApiError):
        return exc.hint or str(exc)
    if isinstance(exc, RateLimitedError):
        return str(exc)
    msg = str(exc or exc.__class__.__name__)
    low = msg.casefold()
    if "human verification" in low or "405" in msg and "download" in low:
        return (
            "Sketchfab WAF blocked the API (Human Verification). "
            "Pause downloads & Find searches, open sketchfab.com in your browser, wait ~10 min, retry."
        )
    if "403" in msg and "forbidden" in low:
        return "Download forbidden (403) — model not downloadable for your token, or author revoked access."
    if "429" in msg or "rate limit" in low:
        return "Sketchfab rate limit — wait a few minutes, set download Parallel to 1, try again."
    if "no download url" in low:
        return "No GLB/GLTF/source URL returned — model may not be downloadable anymore."
    return msg[:240]


def _http_error_hint(resp: requests.Response) -> str:
    code = int(getattr(resp, "status_code", 0) or 0)
    body = (getattr(resp, "text", None) or "")[:800]
    low = body.casefold()
    if code == 405 and "human verification" in low:
        return (
            "Sketchfab WAF blocked the API (405 Human Verification). "
            "Too many requests — open sketchfab.com in Chrome, wait ~10 minutes, "
            "set Downloads Parallel to 1, then retry."
        )
    if code == 403:
        return (
            "Forbidden (403) — download not allowed for your account on this model "
            "(revoked, private, or stale Downloadable=Yes in workbook)."
        )
    if code == 401:
        return "Unauthorized (401) — check SKETCHFAB_TOKEN in .env."
    if code == 404:
        return "Not found (404) — model removed or private."
    if code == 429:
        return "Rate limited (429) — wait and retry with fewer parallel API calls."
    return ""


@dataclass
class Model:
    uid: str
    name: str
    tags: list[str]
    author: str | None
    is_downloadable: bool | None

@dataclass
class Collection:
    uid: str
    name: str
    slug: str | None

class SketchfabClient:
    """All instances share write pacing so Push + Create don't stampede in parallel."""

    _gate = threading.RLock()
    _last_post_at = 0.0
    _last_get_at_by_lane: dict[str, float] = {"api": 0.0, "download": 0.0, "push": 0.0}
    _get_pace_scale_by_lane: dict[str, float] = {"api": 1.0, "download": 1.0, "push": 1.0}
    _post_pace_scale = 1.0
    _post_cooldown_until = 0.0
    _last_throttle_at = 0.0
    _pace_ease_steps_done = 0
    throttle_waits = 0
    _waf_until = 0.0

    def __init__(self, token: str | None = None, api_base: str = API_BASE, *, pace_lane: str = "api"):
        self.api_base = api_base
        self.token = token or TOKEN
        self._pace_lane = pace_lane if pace_lane in ("download", "push") else "api"
        if not self.token:
            raise RuntimeError("SKETCHFAB_TOKEN not set (env or .env).")
        self.sess = requests.Session()
        ua = os.environ.get("SKETCHFAB_USER_AGENT") or "SketchfabCollectionsDesktop/1.0"
        self.sess.headers.update({
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
            "User-Agent": ua,
        })

    @classmethod
    def for_download(cls, token: str | None = None) -> SketchfabClient:
        """Separate GET pacing from browse/collect/find — normal DL shouldn't inherit their backoff."""
        return cls(token=token, pace_lane="download")

    @classmethod
    def for_push(cls, token: str | None = None) -> SketchfabClient:
        """Conservative POST pacing for bulk Push — separate from browse/create lane."""
        return cls(token=token, pace_lane="push")

    @classmethod
    def reset_download_lane(cls) -> None:
        with cls._gate:
            cls._get_pace_scale_by_lane["download"] = 1.0
            cls._last_get_at_by_lane["download"] = 0.0

    def _get_lane_scale(self) -> float:
        with self._gate:
            return float(type(self)._get_pace_scale_by_lane.get(self._pace_lane, 1.0))

    def _is_write(self, method: str) -> bool:
        return method.upper() in _WRITE_METHODS

    def seconds_until_writable(self) -> float:
        with self._gate:
            return max(0.0, self._post_cooldown_until - time.time())

    def _maybe_ease_post_pace(self) -> None:
        """Periodically ease ×N pacing when Sketchfab has stopped 429'ing."""
        if self._pace_lane == "push":
            with self._gate:
                cur = type(self)._get_pace_scale_by_lane.get("push", 1.0)
            if cur <= 1.01:
                return
            now = time.time()
            with self._gate:
                since = now - (self._last_throttle_at or now)
            steps = int(since // 20)
            if steps <= self._pace_ease_steps_done:
                return
            with self._gate:
                for _ in range(steps - self._pace_ease_steps_done):
                    type(self)._get_pace_scale_by_lane["push"] = max(
                        1.0, type(self)._get_pace_scale_by_lane.get("push", 1.0) * 0.88
                    )
                type(self)._pace_ease_steps_done = steps
            return
        if self._post_pace_scale <= 1.01:
            return
        now = time.time()
        since = now - (self._last_throttle_at or now)
        steps = int(since // 20)
        if steps <= self._pace_ease_steps_done:
            return
        for _ in range(steps - self._pace_ease_steps_done):
            type(self)._post_pace_scale = max(1.0, self._post_pace_scale * 0.82)
        type(self)._pace_ease_steps_done = steps
        if now < self._post_cooldown_until and since >= 20:
            type(self)._post_cooldown_until = min(
                self._post_cooldown_until,
                now + (MIN_POST_INTERVAL_SEC * self._post_pace_scale),
            )
        logger.info(
            "Easing post pace to ×%.1f after %.0fs without 429",
            self._post_pace_scale,
            since,
        )

    def _pace(self, method: str) -> None:
        is_write = self._is_write(method)
        lane = self._pace_lane
        while True:
            with self._gate:
                # WAF cooldown applies to bulk API lane only — browser works, downloads can retry sooner.
                waf_wait = 0.0
                if lane == "api":
                    waf_wait = max(0.0, type(self)._waf_until - time.time())
                if is_write:
                    self._maybe_ease_post_pace()
                now = time.time()
                cooldown = max(0.0, self._post_cooldown_until - now) if is_write else 0.0
                if is_write:
                    last = self._last_post_at
                    scale = self._post_pace_scale if self._pace_lane != "push" else type(self)._get_pace_scale_by_lane.get("push", 1.0)
                    if self._pace_lane == "push":
                        interval = PUSH_MIN_POST_INTERVAL_SEC * scale
                    else:
                        interval = MIN_POST_INTERVAL_SEC * scale
                else:
                    last = type(self)._last_get_at_by_lane.get(lane, 0.0)
                    scale = type(self)._get_pace_scale_by_lane.get(lane, 1.0)
                    base = MIN_GET_INTERVAL_DOWNLOAD_SEC if lane == "download" else MIN_GET_INTERVAL_SEC
                    interval = base * scale
                gap = max(0.0, interval - (now - last))
                wait = max(cooldown, gap, waf_wait)
            if wait <= 0:
                return
            time.sleep(min(wait, 2.0 if lane == "download" else 5.0))

    def _mark_request(self, method: str) -> None:
        with self._gate:
            if self._is_write(method):
                type(self)._last_post_at = time.time()
            else:
                type(self)._last_get_at_by_lane[self._pace_lane] = time.time()

    def _note_throttled(self, method: str, wait: float) -> None:
        with self._gate:
            type(self).throttle_waits += 1
            if self._is_write(method):
                type(self)._last_throttle_at = time.time()
                type(self)._pace_ease_steps_done = 0
                bump = 1.22 if self._pace_lane == "push" else 1.35
                cap = 3.0 if self._pace_lane == "push" else 8.0
                if self._pace_lane == "push":
                    cur = type(self)._get_pace_scale_by_lane.get("push", 1.0)
                    type(self)._get_pace_scale_by_lane["push"] = min(cur * bump, cap)
                    scale = type(self)._get_pace_scale_by_lane["push"]
                else:
                    type(self)._post_pace_scale = min(self._post_pace_scale * bump, cap)
                    scale = self._post_pace_scale
                type(self)._post_cooldown_until = (
                    time.time() + wait + (PUSH_MIN_POST_INTERVAL_SEC if self._pace_lane == "push" else MIN_POST_INTERVAL_SEC) * scale
                )
            else:
                lane = self._pace_lane
                cur = type(self)._get_pace_scale_by_lane.get(lane, 1.0)
                cap = 4.0 if lane == "download" else 8.0
                type(self)._get_pace_scale_by_lane[lane] = min(cur * 1.35, cap)

    def _note_success(self, method: str) -> None:
        with self._gate:
            if self._is_write(method):
                if self._pace_lane == "push":
                    cur = type(self)._get_pace_scale_by_lane.get("push", 1.0)
                    type(self)._get_pace_scale_by_lane["push"] = max(1.0, cur * 0.96)
                else:
                    type(self)._post_pace_scale = max(1.0, self._post_pace_scale * 0.92)
            else:
                lane = self._pace_lane
                cur = type(self)._get_pace_scale_by_lane.get(lane, 1.0)
                type(self)._get_pace_scale_by_lane[lane] = max(1.0, cur * 0.82)

    def _retry_wait(self, attempt: int, resp: requests.Response, *, cap: float = 90.0) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 2.0)
            except ValueError:
                pass
        return min(4.0 * (2 ** attempt), cap)

    def _fail_response(self, method: str, url: str, resp: requests.Response) -> None:
        hint = _http_error_hint(resp)
        code = int(resp.status_code)
        if code == 405 and "human verification" in (resp.text or "").casefold():
            with self._gate:
                if self._pace_lane == "api":
                    type(self)._waf_until = max(type(self)._waf_until, time.time() + 120.0)
                    cur = type(self)._get_pace_scale_by_lane.get("api", 1.0)
                    type(self)._get_pace_scale_by_lane["api"] = min(4.0, max(cur, 2.0))
        if hint:
            raise SketchfabApiError(f"HTTP {code} {method} {url}", status=code, hint=hint)
        resp.raise_for_status()

    # --- HTTP ---
    def _request(
        self,
        method: str,
        path: str,
        *,
        max_retries: int | None = None,
        retry_wait_cap: float = 90.0,
        **kwargs,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        retries = MAX_REQUEST_RETRIES if max_retries is None else max(1, int(max_retries))
        resp: requests.Response | None = None
        last_wait = 0.0
        for attempt in range(retries):
            self._pace(method)
            headers = {"Content-Type": "application/json"} if self._is_write(method) else None
            resp = self.sess.request(method, url, timeout=30, headers=headers, **kwargs)
            self._mark_request(method)

            if resp.status_code in (429, 500, 502, 503, 504):
                wait = self._retry_wait(attempt, resp, cap=retry_wait_cap)
                last_wait = wait
                self._note_throttled(method, wait)
                logger.warning(
                    "HTTP %s throttled (%s). Sleeping %.1fs (attempt %d/%d, lane %s pace ×%.1f)",
                    method,
                    resp.status_code,
                    wait,
                    attempt + 1,
                    retries,
                    self._pace_lane if not self._is_write(method) else "write",
                    self._post_pace_scale if self._is_write(method) else self._get_lane_scale(),
                )
                time.sleep(wait)
                continue
            if resp.ok:
                self._note_success(method)
                return resp
            logger.error("HTTP %s %s failed: %s %s", method, url, resp.status_code, resp.text[:500])
            self._fail_response(method, url, resp)
        if resp is not None and resp.status_code == 429:
            raise RateLimitedError(
                f"Sketchfab rate limit (429) after {retries} tries — wait a few minutes.",
                retry_after=last_wait or self.seconds_until_writable(),
            )
        if resp is not None:
            resp.raise_for_status()
        raise RuntimeError(f"Request failed without response: {method} {url}")

    def get_me(self) -> dict:
        resp = self._request("GET", "/me")
        return resp.json()

    def get_liked_models(self, progress: bool = False) -> list[Model]:
        models: list[Model] = []
        url = f"{self.api_base}/me/likes"
        page = 1
        total = 0
        while url:
            resp = self._request("GET", url)
            data = resp.json()
            results = data.get("results", [])
            for m in results:
                uid = m.get("uid") or m.get("model", {}).get("uid") or m.get("uid")
                name = m.get("name") or m.get("model", {}).get("name") or ""
                tags = [t["name"] if isinstance(t, dict) else str(t) for t in (m.get("tags") or m.get("model", {}).get("tags") or [])]
                author = (m.get("user") or m.get("model", {}).get("user") or {}).get("displayName")
                downloadable = (m.get("isDownloadable") if "isDownloadable" in m else (m.get("model", {}).get("isDownloadable")))
                if not uid:
                    continue
                models.append(Model(uid=uid, name=name, tags=tags, author=author, is_downloadable=downloadable))
            total += len(results)
            if progress:
                logger.info("Fetched likes page %d: %d items (running total: %d)", page, len(results), total)
            page += 1
            url = data.get("next")
        return models

    def get_collections(self, progress: bool = False) -> list[Collection]:
        cols: list[Collection] = []
        url = f"{self.api_base}/me/collections"
        page = 1
        total = 0
        while url:
            resp = self._request("GET", url)
            data = resp.json()
            results = data.get("results", [])
            for c in results:
                cols.append(Collection(uid=c.get("uid"), name=c.get("name"), slug=c.get("slug")))
            total += len(results)
            if progress:
                logger.info("Fetched collections page %d: %d items (running total: %d)", page, len(results), total)
            page += 1
            url = data.get("next")
        return cols

    def create_collection(self, name: str, model_uids: list[str] | None = None) -> Collection:
        # Sketchfab rejects empty creates: {"models":["This field is required."]}
        uids = [str(u).strip() for u in (model_uids or []) if str(u).strip()]
        if not uids:
            raise ValueError("Sketchfab requires at least one model when creating a collection.")
        payload: dict[str, t.Any] = {"name": name.strip(), "models": uids}
        # Interactive create: few retries, shorter sleeps — don't block the UI for 15+ minutes.
        resp = self._request(
            "POST",
            "/collections",
            json=payload,
            max_retries=MAX_CREATE_RETRIES,
            retry_wait_cap=30.0,
        )
        location = resp.headers.get("Location", "")
        if location:
            detail = self._request("GET", location).json()
            return Collection(
                uid=str(detail.get("uid") or ""),
                name=str(detail.get("name") or name),
                slug=detail.get("slug"),
            )
        data = resp.json() if resp.content else {}
        return Collection(
            uid=str(data.get("uid") or ""),
            name=str(data.get("name") or name),
            slug=data.get("slug"),
        )

    def list_models_in_collection(self, collection_uid: str) -> list[str]:
        """List model UIDs in a collection.

        Always pass restricted=1 — without it Sketchfab hides age-restricted
        models from the results even when they are actually in the collection
        (POST returns 201, then a default list looks empty for those UIDs).
        """
        uids: list[str] = []
        url = f"{self.api_base}/collections/{collection_uid}/models?per_page=100&restricted=1"
        while url:
            resp = self._request("GET", url)
            data = resp.json()
            for item in data.get("results", []):
                uid = item.get("uid") or (item.get("model") or {}).get("uid")
                if uid:
                    uids.append(uid)
            url = data.get("next")
        return uids

    def list_collection_models(
        self,
        collection_uid: str,
        *,
        count: int = 24,
        cursor_url: str | None = None,
        sort_by: str = "",
        downloadable: bool | None = None,
    ) -> dict:
        """Paginated models in a collection (full model dicts). Always uses restricted=1.

        sort_by: Sketchfab accepts -likeCount, -viewCount, -publishedAt, etc.
        downloadable: when True, only downloadable models (API ``downloadable=1``).
        """
        if cursor_url:
            resp = self._request("GET", cursor_url)
        else:
            params: dict[str, t.Any] = {
                "per_page": min(100, max(1, count)),
                "restricted": 1,
            }
            sb = (sort_by or "").strip()
            if sb and sb not in {"order", "default", ""}:
                params["sort_by"] = sb
            if downloadable:
                params["downloadable"] = 1
            resp = self._request(
                "GET",
                f"/collections/{collection_uid}/models",
                params=params,
            )
        return resp.json()

    def get_collection(self, collection_uid: str) -> dict:
        resp = self._request("GET", f"/collections/{collection_uid}")
        return resp.json()

    def search_collections(
        self,
        *,
        query: str = "",
        user: str = "",
        sort_by: str = "subscriberCount",
        date: int | None = None,
        count: int = 24,
        cursor_url: str | None = None,
    ) -> dict:
        """Search public collections via /v3/search?type=collections."""
        if cursor_url:
            resp = self._request("GET", cursor_url)
        else:
            params: dict[str, t.Any] = {"type": "collections", "count": min(24, max(1, count))}
            if query.strip():
                params["q"] = query.strip()
            if user.strip():
                params["user"] = user.strip()
            if sort_by:
                params["sort_by"] = sort_by
            if date is not None and int(date) in (1, 7, 31):
                params["date"] = int(date)
            resp = self._request("GET", "/search", params=params)
        return resp.json()

    def subscribe_collection(self, collection_uid: str) -> None:
        uid = str(collection_uid or "").strip()
        if not uid:
            return
        self._request("POST", "/me/subscriptions", json={"collection": uid})

    def unsubscribe_collection(self, collection_uid: str) -> None:
        uid = str(collection_uid or "").strip()
        if not uid:
            return
        self._request("DELETE", f"/me/subscriptions/{uid}")

    def add_model_to_collection(self, collection_uid: str, model_uid: str) -> None:
        self.add_models_to_collection(collection_uid, [model_uid])

    def add_models_to_collection(self, collection_uid: str, model_uids: list[str]) -> int:
        uids = [str(u).strip() for u in model_uids if str(u).strip()]
        if not uids:
            return 0
        self._request(
            "POST",
            f"/collections/{collection_uid}/models",
            json={"models": uids},
        )
        return len(uids)

    def remove_model_from_collection(self, collection_uid: str, model_uid: str) -> None:
        self.remove_models_from_collection(collection_uid, [model_uid])

    def remove_models_from_collection(self, collection_uid: str, model_uids: list[str]) -> None:
        uids = [str(u).strip() for u in model_uids if str(u).strip()]
        if not uids:
            return
        self._request(
            "DELETE",
            f"/collections/{collection_uid}/models",
            json={"models": uids},
        )

    def get_model(self, model_uid: str) -> dict:
        resp = self._request("GET", f"/models/{model_uid}")
        return resp.json()

    def search_models(
        self,
        *,
        query: str = "",
        sort_by: str = "-viewCount",
        downloadable: bool | None = None,
        staffpicked: bool | None = None,
        animated: bool | None = None,
        categories: str | None = None,
        date: int | None = None,
        count: int = 24,
        cursor_url: str | None = None,
        max_retries: int | None = None,
        retry_wait_cap: float = 25.0,
    ) -> dict:
        """Search public catalog via /v3/search?type=models. Returns API page dict."""
        req_kw = {}
        if max_retries is not None:
            req_kw["max_retries"] = max_retries
            req_kw["retry_wait_cap"] = retry_wait_cap
        if cursor_url:
            resp = self._request("GET", cursor_url, **req_kw)
        else:
            params: dict[str, t.Any] = {"type": "models", "count": min(24, max(1, count))}
            if query.strip():
                params["q"] = query.strip()
            if sort_by:
                params["sort_by"] = sort_by
            if downloadable is True:
                params["downloadable"] = "true"
            if staffpicked is True:
                params["staffpicked"] = "true"
            if animated is True:
                params["animated"] = "true"
            if categories:
                params["categories"] = categories
            # Sketchfab search only accepts date=1, 7, or 31
            if date is not None and int(date) in (1, 7, 31):
                params["date"] = int(date)
            resp = self._request("GET", "/search", params=params, **req_kw)
        return resp.json()

    def like_model(self, model_uid: str) -> None:
        self._request("POST", "/me/likes", json={"model": model_uid})

    def follow_user(self, user_uid: str) -> None:
        uid = str(user_uid or "").strip()
        if not uid:
            raise ValueError("user uid required")
        self._request("POST", "/me/followings", json={"toUser": uid})

    def unfollow_user(self, user_uid: str) -> None:
        uid = str(user_uid or "").strip()
        if not uid:
            raise ValueError("user uid required")
        self._request("DELETE", "/me/followings", json={"toUser": uid})

    def list_my_followings(self, *, count: int = 24, cursor_url: str | None = None) -> dict:
        if cursor_url:
            resp = self._request("GET", cursor_url)
        else:
            resp = self._request("GET", "/me/followings", params={"count": min(24, max(1, count))})
        return resp.json()

    def is_following_user(self, user_uid: str, *, max_pages: int = 20) -> bool:
        """True if authenticated user follows user_uid (scans /me/followings)."""
        want = str(user_uid or "").strip().casefold()
        if not want:
            return False
        cursor = None
        for _ in range(max(1, int(max_pages))):
            data = self.list_my_followings(count=24, cursor_url=cursor)
            for u in data.get("results") or []:
                if str(u.get("uid") or "").strip().casefold() == want:
                    return True
            cursor = data.get("next")
            if not cursor:
                break
        return False

    def unlike_model(self, model_uid: str) -> None:
        """Remove a like (DELETE /me/likes/{uid}). Does not change collection membership."""
        uid = str(model_uid or "").strip()
        if not uid:
            return
        self._request("DELETE", f"/me/likes/{uid}")

    def get_categories(self) -> list[dict]:
        resp = self._request("GET", "/categories")
        return resp.json().get("results") or []

    def get_model_download(self, model_uid: str) -> dict:
        resp = self._request(
            "GET",
            f"/models/{model_uid}/download",
            max_retries=2,
            retry_wait_cap=8.0,
        )
        return resp.json()

    def load_my_following_uids(self, *, max_pages: int = 80) -> set[str]:
        """All user UIDs the authenticated account follows (for Find follow buttons)."""
        out: set[str] = set()
        cursor: str | None = None
        for _ in range(max(1, int(max_pages))):
            data = self.list_my_followings(count=24, cursor_url=cursor)
            for u in data.get("results") or []:
                uid = str(u.get("uid") or "").strip()
                if uid:
                    out.add(uid)
            cursor = data.get("next")
            if not cursor:
                break
        return out

    def get_user(self, username_or_uid: str) -> dict:
        """Resolve a public user profile (uid preferred; username via search fallback).

        Never returns a fuzzy displayName hit — that mapped deleted accounts (e.g. hyde)
        onto unrelated users who happen to share a display name.
        """
        key = (username_or_uid or "").strip()
        if not key:
            raise ValueError("username or uid required")
        try:
            url = f"{self.api_base}/users/{key}"
            self._pace("GET")
            resp = self.sess.get(url, timeout=30)
            self._mark_request("GET")
            if resp.ok:
                return resp.json()
        except Exception:
            pass
        data = self.search_users(query=key, count=24)
        results = data.get("results") or []
        key_cf = key.casefold()

        def _uname(u: dict) -> str:
            raw = str(u.get("username") or "").strip()
            if raw:
                return raw
            profile = str(u.get("profileUrl") or "")
            if "sketchfab.com/" in profile:
                return profile.rstrip("/").split("sketchfab.com/")[-1].split("/")[0]
            return ""

        for u in results:
            if str(u.get("uid") or "").casefold() == key_cf:
                return u
        for u in results:
            if _uname(u).casefold() == key_cf:
                return u
        for u in results:
            if str(u.get("displayName") or "").casefold() == key_cf:
                return u
        raise LookupError(
            f"User not found: {key}"
            + (" (profile may be deleted — try a model UID from Likes)" if results else "")
        )

    def search_users(
        self,
        *,
        query: str = "",
        sort_by: str = "",
        count: int = 12,
        cursor_url: str | None = None,
    ) -> dict:
        """Search public users via /v3/search?type=users.

        sort_by examples: -followerCount, -modelCount (API-native).
        Likes/collections sorts are unreliable server-side — sort client-side instead.
        """
        if cursor_url:
            resp = self._request("GET", cursor_url)
        else:
            params: dict[str, t.Any] = {"type": "users", "count": min(24, max(1, count))}
            if query.strip():
                params["q"] = query.strip()
            if sort_by.strip():
                params["sort_by"] = sort_by.strip()
            resp = self._request("GET", "/search", params=params)
        return resp.json()

    def list_user_models(
        self,
        username: str,
        *,
        count: int = 24,
        cursor_url: str | None = None,
        sort_by: str = "-publishedAt",
    ) -> dict:
        """Public models posted by username (`/models?user=`)."""
        if cursor_url:
            resp = self._request("GET", cursor_url)
        else:
            params: dict[str, t.Any] = {
                "user": username.strip(),
                "count": min(24, max(1, count)),
            }
            if sort_by:
                params["sort_by"] = sort_by
            resp = self._request("GET", "/models", params=params)
        return resp.json()

    def list_user_likes(
        self,
        username: str,
        *,
        count: int = 24,
        cursor_url: str | None = None,
    ) -> dict:
        """Public likes for username (`/models?liked_by=`)."""
        if cursor_url:
            resp = self._request("GET", cursor_url)
        else:
            resp = self._request(
                "GET",
                "/models",
                params={"liked_by": username.strip(), "count": min(24, max(1, count))},
            )
        return resp.json()

    def list_user_collections(
        self,
        username: str,
        *,
        count: int = 24,
        cursor_url: str | None = None,
    ) -> dict:
        """Public collections owned by username.

        Prefer `/search?type=collections&user=` — `/collections?by=` often returns
        unrelated popular collections (and our owner filter then shows "none").
        Still keep an owner-username filter as a safety net.
        """
        data = self.search_collections(
            user=username.strip(),
            sort_by="-createdAt",
            count=count,
            cursor_url=cursor_url,
        )
        want = username.strip().casefold()
        if not want:
            return data
        kept = []
        for item in data.get("results") or []:
            c = item.get("collection") if isinstance(item.get("collection"), dict) else item
            if not isinstance(c, dict):
                continue
            user = c.get("user") or c.get("owner") or {}
            un = str((user or {}).get("username") or "").strip().casefold()
            if un == want:
                kept.append(item)
        out = dict(data)
        out["results"] = kept
        if not kept:
            out["next"] = None
            out["cursors"] = {}
        return out

    def list_my_subscriptions(self, *, count: int = 24, cursor_url: str | None = None) -> dict:
        """Authenticated user's collection subscriptions (`/me/subscriptions`)."""
        if cursor_url:
            resp = self._request("GET", cursor_url)
        else:
            resp = self._request("GET", "/me/subscriptions", params={"count": min(24, max(1, count))})
        return resp.json()

    def model_exists(self, model_uid: str) -> bool | None:
        """True if public model still fetchable; False if 404; None on other errors."""
        uid = (model_uid or "").strip()
        if not uid:
            return False
        try:
            self._pace("GET")
            resp = self.sess.get(f"{self.api_base}/models/{uid}", timeout=20)
            self._mark_request("GET")
            if resp.status_code == 404:
                return False
            if resp.ok:
                return True
            return None
        except Exception:
            return None

    @staticmethod
    def embed_url(model_uid: str) -> str:
        return f"https://sketchfab.com/models/{model_uid}/embed?autostart=1&ui_theme=dark"
