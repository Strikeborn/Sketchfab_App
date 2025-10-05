from __future__ import annotations
import os
import time
import typing as t
import logging
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

API_BASE = os.environ.get("SKETCHFAB_API_BASE", "https://api.sketchfab.com/v3")
TOKEN = os.environ.get("SKETCHFAB_TOKEN")

MIN_POST_INTERVAL_SEC = float(os.environ.get("MIN_POST_INTERVAL_SEC", "1.0"))

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
    def __init__(self, token: str | None = None, api_base: str = API_BASE):
        self.api_base = api_base
        self.token = token or TOKEN
        if not self.token:
            raise RuntimeError("SKETCHFAB_TOKEN not set (env or .env).")
        self._last_post_at = 0.0
        self.sess = requests.Session()
        self.sess.headers.update({
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Sketchfab-Collections-Pipeline/5.0"
        })

    # --- HTTP ---
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        for attempt in range(5):
            # Enforce min delay for POST-like methods
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                elapsed = time.time() - self._last_post_at
                if elapsed < MIN_POST_INTERVAL_SEC:
                    time.sleep(MIN_POST_INTERVAL_SEC - elapsed)

            resp = self.sess.request(method, url, timeout=30, **kwargs)

            # Record POST time after request completes (regardless of status)
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                self._last_post_at = time.time()

            if resp.status_code in (429, 502, 503, 504):
                wait = float(resp.headers.get("Retry-After", 1 + attempt))
                logger.warning("HTTP %s to %s throttled (%s). Sleeping %.2fs", method, url, resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.ok:
                return resp
            # Non-retry error
            logger.error("HTTP %s %s failed: %s %s", method, url, resp.status_code, resp.text[:500])
            resp.raise_for_status()
        # If loop exits without return, raise last
        resp.raise_for_status()

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

    def list_models_in_collection(self, collection_uid: str) -> list[str]:
        uids: list[str] = []
        url = f"{self.api_base}/collections/{collection_uid}/items"
        while url:
            resp = self._request("GET", url)
            data = resp.json()
            for item in data.get("results", []):
                uid = (item.get("model") or {}).get("uid")
                if uid:
                    uids.append(uid)
            url = data.get("next")
        return uids

    def add_model_to_collection(self, collection_uid: str, model_uid: str) -> None:
        self._request("POST", f"/collections/{collection_uid}/items", json={"model": model_uid})

    def remove_model_from_collection(self, collection_uid: str, model_uid: str) -> None:
        self._request("DELETE", f"/collections/{collection_uid}/items/{model_uid}")