# main.py — Flet desktop app
# encoding: utf-8
from __future__ import annotations

import os
import sys
import traceback
import pathlib
import shutil
import threading
import asyncio
import logging
import time

# Optional one-time UTF-16 repair (slow on large trees — not every startup).
_root = pathlib.Path(__file__).resolve().parent.parent
if os.environ.get("SKETCHFAB_FIX_ENCODING", "").strip().lower() in {"1", "true", "yes"}:
    for _pc in _root.rglob("__pycache__"):
        shutil.rmtree(_pc, ignore_errors=True)
    for _p in list(_root.rglob("*.py")) + list(_root.rglob("*.bat")):
        try:
            _b = _p.read_bytes()
            if len(_b) >= 2 and _b[:2] == b"\xff\xfe":
                _p.write_text(_b.decode("utf-16"), encoding="utf-8", newline="\n")
            elif b"\x00" in _b:
                _p.write_text(_b.decode("utf-16-le"), encoding="utf-8", newline="\n")
        except OSError:
            pass

ROOT = str(_root)
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from version import APP_VERSION
from session_log import activity as log_activity, end_session, setup_session_log

setup_session_log(ROOT, version=APP_VERSION)

import flet as ft
import pandas as pd

from collection_urls import set_sketchfab_username
from state import (
    AppState,
    normalize_liked_columns,
    _coerce_str_series,
    enrich_download_status,
    mark_downloaded_inplace,
    download_dirs_status,
    ensure_download_dirs,
    uid_col,
)
from ui.toolbar import build_toolbar
from ui.tabs_liked import LikedTab
from ui.tabs_browse import BrowseTab
from ui.tabs_account import AccountTab
from ui.tabs_auto_assign import AutoAssignTab
from ui.tabs_pending import PendingPushTab
from ui.tabs_collections import CollectionsTab
from ui.tabs_browse_collections import BrowseCollectionsTab
from ui.tabs_subscribed import SubscribedTab
from ui.tabs_report import ReportTab
from ui.tabs_changelog import ChangelogTab
from ui.tabs_inspector import InspectorTab
from ui.tabs_settings import SettingsTab
from ui.window_chrome import (
    build_frameless_title_bar,
    configure_window,
    hide_to_tray,
    make_tray_ui_queue,
    pick_caption_mode,
    restore_from_tray,
    start_tray_icon,
    stop_tray_icon,
)
from console_win import set_console_visible
from window_win import force_exit, kill_stuck_flet_windows, relayout_suppressed
from ui.viewer_dock import close_viewer_app, open_viewer_app
from ui.viewer_server import set_model, start_server, stop_server, viewer_url as build_viewer_url
from ui.log_view import LogView
from ui.selection_detail import SelectionDetailPane
from ui.download_queue import DownloadQueuePane
from ui.flet_safe import safe_page_update
from ui.pager import build_pager
from data_io import read_workbook, write_workbook, append_browse_results, XL_PATH
from collector import build_workbook, build_workbook_quick, refresh_subscriptions_sheet
from app_settings import load_settings, save_settings
from auto_assign import run_auto_assign
from push_assignments import pending_push_rows, push
from sketchfab_client import SketchfabClient

logger = logging.getLogger(__name__)
from merge_collections import find_similar_collections
from assign_actions import assign_models, clear_assignment_inplace, strip_already_in_inplace
from download_model import (
    download_glb,
    collections_for_download,
    safe_collection_folder,
    resolve_model_dir,
    organize_downloads_by_collection,
)
from browse_presets import load_presets, save_presets
from terms_yaml_edit import append_tag_to_collection
from version import APP_VERSION
from sync_status import format_duration
from sync_probe import format_probe_summary, run_sync_probe
from content_filter_ctx import broadcast_content_filters, flags_from_state
import perf_log


def _noneify(df: pd.DataFrame) -> pd.DataFrame:
    return df.where(pd.notna(df), None)


class Ref:
    def __init__(self, value=None):
        self.value = value

    def set(self, v):
        self.value = v


def main(page: ft.Page):
    t_app_start = time.perf_counter()
    stop_tray_icon()
    app_settings = load_settings()
    page.title = f"Sketchfab Collections — Desktop  v{APP_VERSION}"
    page.theme_mode = ft.ThemeMode.DARK
    dual_close = bool(
        app_settings.get(
            "dual_close_buttons",
            app_settings.get("custom_window_caption", True),
        )
    )
    caption_mode = pick_caption_mode(dual_close=dual_close)
    configure_window(page, mode=caption_mode)
    page.window.maximized = True

    state = AppState()
    job_cancel = threading.Event()
    log = LogView()
    detail_pane = SelectionDetailPane()
    dl_queue = DownloadQueuePane()
    liked_tab = LikedTab(page)
    browse_tab = BrowseTab(page)
    account_tab = AccountTab(page)
    pending_tab = PendingPushTab(page)
    auto_assign_tab = AutoAssignTab(page)
    colls_tab = CollectionsTab()
    browse_colls_tab = BrowseCollectionsTab(page)
    subs_tab = SubscribedTab()
    tabs_ctrl: list[ft.Tabs | None] = [None]

    def open_collection_tab(name: str) -> None:
        colls_tab.set_search_query(name)
        if tabs_ctrl[0] is not None:
            tabs_ctrl[0].selected_index = 6  # My Collections
        page.update()

    def go_to_my_collection(meta: dict) -> None:
        if tabs_ctrl[0] is not None:
            tabs_ctrl[0].selected_index = 6
            try:
                tabs_ctrl[0].update()
            except Exception:
                pass
        try:
            colls_tab.open_by_meta(meta)
        except Exception as e:
            info(f"Open My Collection failed: {e}")
        page.update()

    def go_to_browse_collection(meta: dict) -> None:
        if tabs_ctrl[0] is not None:
            tabs_ctrl[0].selected_index = 7  # Browse Collections
            try:
                tabs_ctrl[0].update()
            except Exception:
                pass
        try:
            browse_colls_tab.open_by_meta(meta)
        except Exception as e:
            info(f"Open Browse Collection failed: {e}")
        page.update()

    def go_to_account(username_or_key: str) -> None:
        """Jump to Account tab and load this user (models/likes/collections/subs)."""
        key = (username_or_key or "").strip()
        if not key:
            return
        # Prefill field before tab change so on_tab_shown does not auto-load Me.
        account_tab._user_field.value = key
        try:
            account_tab._user_field.update()
        except Exception:
            pass
        if tabs_ctrl[0] is not None:
            tabs_ctrl[0].selected_index = 5  # Account
            try:
                tabs_ctrl[0].update()
            except Exception:
                pass
        account_tab.open_user(key)
        page.update()

    def do_vet_term(term: str, status: str) -> None:
        info(f"Tag '{term}' marked {status} (saved to data/tag_vetting.yaml).")
        refresh_tables()

    def do_add_tag_yaml(term: str, collection: str) -> None:
        if append_tag_to_collection(term, collection):
            info(f"Added tag '{term}' to '{collection}' in collections_terms.yaml.")
        else:
            info(f"Could not add '{term}' to YAML.")

    report_tab = ReportTab(
        on_find_similar=lambda: do_merge(),
        on_vet_term=do_vet_term,
        on_open_collection=open_collection_tab,
        on_add_tag_yaml=do_add_tag_yaml,
    )
    changelog_tab = ChangelogTab()
    inspector_tab = InspectorTab(page)
    settings_tab = SettingsTab()
    sync_drift_ref = Ref("")
    _bg_sync_timer: list[threading.Timer | None] = [None]
    _shutting_down = [False]
    _tray_ui_queue = make_tray_ui_queue()
    _tray_ready = [False]

    overwrite_ref = Ref(False)
    dry_run_ref = Ref(bool(app_settings.get("dry_run_default", False)))
    auto_push_ref = Ref(False)
    hide_nsfw_ref = Ref(True)
    hide_female_ref = Ref(True)
    hide_male_ref = Ref(True)
    counts = ft.Text("", size=13)

    def refresh_counts():
        dirs_txt = download_dirs_status()
        downloaded = 0
        if not state.liked_df.empty and "Downloaded" in state.liked_df.columns:
            downloaded = int((state.liked_df["Downloaded"].astype(str).str.lower() == "yes").sum())
        unpushed = 0
        try:
            unpushed = len({r["uid"] for r in pending_push_rows(state.liked_df)})
        except Exception:
            unpushed = 0
        try:
            from sync_status import collected_ago_text

            collected_txt = collected_ago_text(state.liked_df)
        except Exception:
            collected_txt = ""
        drift = (sync_drift_ref.value or "").strip()
        drift_txt = f"  ·  {drift}" if drift else ""
        counts.value = (
            f"Liked {len(state.liked_df):,}  ·  Colls {len(state.colls_df):,}  ·  "
            f"Subs {len(state.subs_df):,}  ·  On disk {downloaded:,}  ·  "
            f"Unpushed {unpushed:,}  ·  {collected_txt}{drift_txt}  ·  Scan: {dirs_txt}"
        )
        counts.update()

    def apply_pagers():
        def change_liked_page(p: int):
            state.liked_page = max(0, p)
            liked_tab.set_df(state.liked_df, state.liked_page, state.liked_page_size, state.colls_df)
            apply_pagers()

        def change_liked_size(s: int):
            state.liked_page_size = s
            state.liked_page = 0
            liked_tab.set_df(state.liked_df, 0, s, state.colls_df)
            apply_pagers()

        def change_colls_page(p: int):
            state.colls_page = max(0, p)
            colls_tab.set_df(state.colls_df, state.colls_page, state.colls_page_size)
            apply_pagers()

        def change_colls_size(s: int):
            state.colls_page_size = s
            state.colls_page = 0
            colls_tab.set_df(state.colls_df, 0, s)
            apply_pagers()

        def change_subs_page(p: int):
            state.subs_page = max(0, p)
            subs_tab.set_df(state.subs_df, state.subs_page, state.subs_page_size)
            apply_pagers()

        def change_subs_size(s: int):
            state.subs_page_size = s
            state.subs_page = 0
            subs_tab.set_df(state.subs_df, 0, s)
            apply_pagers()

        filtered_total = len(liked_tab._filtered)
        liked_pager = build_pager(
            total_rows=filtered_total,
            page=state.liked_page,
            page_size=state.liked_page_size,
            on_change_page=change_liked_page,
            on_change_size=change_liked_size,
            show_slider=False,
            compact=True,
        )
        colls_pager = build_pager(
            total_rows=len(state.colls_df),
            page=state.colls_page,
            page_size=state.colls_page_size,
            on_change_page=change_colls_page,
            on_change_size=change_colls_size,
            show_slider=False,
        )
        subs_pager = build_pager(
            total_rows=len(state.subs_df),
            page=state.subs_page,
            page_size=state.subs_page_size,
            on_change_page=change_subs_page,
            on_change_size=change_subs_size,
            show_slider=False,
        )
        liked_tab.pager.controls = [liked_pager]
        colls_tab.pager.controls = [colls_pager]
        subs_tab.pager.controls = [subs_pager]
        for tab in (liked_tab, colls_tab, subs_tab):
            try:
                if getattr(tab, "page", None) is not None:
                    tab.update()
            except (AssertionError, RuntimeError, Exception):
                pass

    def _on_liked_filter(_=0):
        state.liked_page = 0
        apply_pagers()

    liked_tab.set_filter_callback(_on_liked_filter)

    def do_assign_model(uid: str, collection: str):
        from assign_actions import assign_models_inplace
        from collection_match import match_collection_name
        from push_assignments import pending_push_rows
        from search_filter import list_collection_names

        # Canonicalize Enemies→Enemy etc. so Push finds the real collection.
        names = list_collection_names(state.colls_df, limit=50_000)
        names_cf = {n.casefold() for n in names}
        resolved = match_collection_name(collection, names) or (collection or "").strip()
        # Write workbook first — chrome-before-write raced with tab switches / set_df
        # and dropped assignments that never hit Assigned Collection(s).
        assign_models_inplace(state.liked_df, [uid], resolved)
        liked_tab.bind_source(state.liked_df)
        liked_tab.apply_assign_instant(uid, resolved)
        pending_tab.mark_dirty(state.liked_df)
        refresh_counts()

        def _work():
            _persist_workbook()
            note = f"Assigned → '{resolved}'"
            if resolved.casefold() != (collection or "").strip().casefold():
                note += f" (matched from '{collection}')"
            uid_s = str(uid).strip()
            still = any(
                r["uid"] == uid_s and r["collection"].casefold() == resolved.casefold()
                for r in pending_push_rows(state.liked_df)
            )
            note += (
                " (local — Push / Pending Push)."
                if still
                else " (already in workbook Already In — nothing new to Push)."
            )
            if resolved.casefold() not in names_cf:
                note += f" '{resolved}' isn't in My Collections yet — Collect or typed create, then Push."
            info(note, quiet=True)
            log.flush()
            _schedule_auto_push()

        threading.Thread(target=_work, daemon=True).start()

    _create_coll_lock = threading.Lock()

    def do_assign_or_create(uids, collection: str):
        """Typed assign: match existing, else create on Sketchfab then assign all selected."""
        from collection_match import match_collection_name
        from search_filter import list_collection_names
        from sketchfab_client import RateLimitedError

        if isinstance(uids, str):
            uids = [uids]
        uids = [str(u).strip() for u in (uids or []) if str(u).strip()]
        raw = (collection or "").strip()
        if not uids or not raw:
            return
        names = list_collection_names(state.colls_df, limit=50_000)
        hit = match_collection_name(raw, names)
        if hit:
            do_bulk_assign(uids, hit)
            return

        wait = SketchfabClient().seconds_until_writable()
        if wait > 45:
            do_bulk_assign(uids, raw)
            info(
                f"Sketchfab still cooling down (~{int(wait)}s). "
                f"Assigned '{raw}' locally on {len(uids):,} model(s) — wait a few minutes, then retry typed create "
                f"or create '{raw}' on the website and Collect."
            )
            return

        if not _create_coll_lock.acquire(blocking=False):
            do_bulk_assign(uids, raw)
            info(f"Another create is in progress — assigned '{raw}' locally on {len(uids):,} model(s). Retry create in a minute.")
            return

        info(f"Creating collection '{raw}' on Sketchfab for {len(uids):,} selected model(s)…")
        first = uids[0]

        def _work():
            err = None
            rate_limited = False
            created_name = raw
            try:
                client = SketchfabClient()
                # Sketchfab requires ≥1 model at create; first goes in now, rest stay Assigned for Push.
                col = client.create_collection(raw, model_uids=[first])
                created_name = (col.name or raw).strip() or raw
                row = {
                    "Collection Name": created_name,
                    "Collection UID": col.uid,
                    "Slug": col.slug or "",
                    "Model Count": 1,
                    "Model Names": "",
                    "Thumbnail": "",
                }
                extra = pd.DataFrame([row])
                if state.colls_df is None or state.colls_df.empty:
                    state.colls_df = extra
                else:
                    state.colls_df = pd.concat([state.colls_df, extra], ignore_index=True)
                from assign_actions import assign_models_inplace
                from push_assignments import apply_push_markers

                # Assign EVERY selected model under the final name (avoids race with a second bulk call).
                assign_models_inplace(state.liked_df, uids, created_name)
                # First model is already on Sketchfab from create — fold into Already In.
                state.liked_df = apply_push_markers(
                    state.liked_df, posted_pairs=[(first, created_name)]
                )
                liked_tab.bind_source(state.liked_df)
                _persist_workbook()
                pending_tab.mark_dirty(state.liked_df)
            except RateLimitedError as exc:
                rate_limited = True
                err = exc
                from assign_actions import assign_models_inplace

                assign_models_inplace(state.liked_df, uids, raw)
                liked_tab.bind_source(state.liked_df)
                _persist_workbook()
                pending_tab.mark_dirty(state.liked_df)
                logger.warning("Create collection rate-limited: %s", exc)
            except Exception as exc:
                err = exc
                logger.exception("Create collection failed")
            finally:
                _create_coll_lock.release()

            async def _finish():
                if rate_limited:
                    for u in uids:
                        try:
                            liked_tab.apply_assign_instant(u, raw)
                        except Exception:
                            pass
                    mins = max(1, int((getattr(err, "retry_after", None) or 120) // 60) or 2)
                    info(
                        f"Rate limited creating '{raw}' — assigned {len(uids):,} model(s) locally. "
                        f"Wait ~{mins}+ min, then type the name again to create on Sketchfab "
                        f"(or create it on the website and Collect)."
                    )
                    pending_tab.mark_dirty(state.liked_df)
                    return
                if err:
                    info(f"Could not create '{raw}': {err}")
                    return
                for u in uids:
                    try:
                        liked_tab.apply_assign_instant(u, created_name)
                    except Exception:
                        pass
                # Refresh collection dropdown only — don't remount the whole liked grid.
                try:
                    liked_tab._refresh_dropdowns()
                    liked_tab.update()
                except Exception:
                    liked_tab.set_df(
                        state.liked_df, state.liked_page, state.liked_page_size, state.colls_df,
                        reset_scroll=False, refresh_dropdowns=True,
                    )
                try:
                    colls_tab.set_df(state.colls_df, state.colls_page, state.colls_page_size)
                except Exception:
                    pass
                pending_tab.mark_dirty(state.liked_df)
                refresh_counts()
                rest = len(uids) - 1
                if rest > 0:
                    info(
                        f"Created '{created_name}' — 1 model already on Sketchfab, "
                        f"{rest:,} more pending Push."
                    )
                else:
                    info(f"Created '{created_name}' with this model on Sketchfab (already synced).")

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_cancel_pending(uid: str, collection: str):
        # Remove just this collection so a model's other pending collections survive.
        # UI first; workbook save coalesced on a background timer (was sync per ×).
        n = clear_assignment_inplace(state.liked_df, [uid], collection=collection or None)
        if not n:
            info("Nothing to cancel.")
            return
        liked_tab.bind_source(state.liked_df)
        try:
            liked_tab.apply_cancel_instant(uid, collection or "")
        except Exception:
            pass
        if not pending_tab.remove_collection_chip(uid, collection or ""):
            pending_tab.mark_dirty(state.liked_df)
        refresh_counts()
        info(f"Cancelled assignment → '{collection}' (not pushed).", quiet=True)
        _schedule_cancel_persist()

    _cancel_persist_timer = {"t": None}

    def _schedule_cancel_persist():
        """Coalesce rapid × cancels into one workbook write."""
        old = _cancel_persist_timer["t"]
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass

        def _fire():
            _persist_workbook()
            log.flush()

        t = threading.Timer(0.35, _fire)
        t.daemon = True
        _cancel_persist_timer["t"] = t
        t.start()

    def do_cancel_all_pending():
        """Clear the entire Pending Push queue in one pass + one workbook write."""
        from assign_actions import clear_assignment_pairs_inplace
        from push_assignments import pending_push_rows

        # Flush any pending single-cancel timer so Cancel All owns the write.
        old = _cancel_persist_timer["t"]
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
            _cancel_persist_timer["t"] = None

        pairs = [(r["uid"], r["collection"]) for r in pending_push_rows(state.liked_df)]
        if not pairs:
            info("No pending assignments to cancel.")
            return
        clear_assignment_pairs_inplace(state.liked_df, pairs)
        liked_tab.bind_source(state.liked_df)
        # Don't loop apply_cancel_instant — hundreds of Props cancels would stall the UI.
        try:
            refresh_liked_soft()
        except Exception:
            pass
        pending_tab.set_df(state.liked_df)
        try:
            auto_assign_tab.set_df(state.liked_df)
        except Exception:
            pass
        refresh_counts()
        info(f"Cancelled {len(pairs):,} pending assignment(s) (not pushed).")

        def _bg():
            _persist_workbook()
            log.flush()

        threading.Thread(target=_bg, daemon=True).start()

    def do_remove_from_collection(uids: list[str], collection: str):
        """DELETE model(s) from a Sketchfab collection and strip Already In locally."""
        from collection_match import match_collection_name
        from search_filter import list_collection_names

        names = list_collection_names(state.colls_df, limit=50_000)
        resolved = match_collection_name(collection, names) or (collection or "").strip()
        if not uids or not resolved:
            info("Pick models and a collection to remove from.")
            return
        # Resolve collection UID from workbook
        coll_uid = ""
        if not state.colls_df.empty and "Collection Name" in state.colls_df.columns:
            for _, row in state.colls_df.iterrows():
                if str(row.get("Collection Name") or "").strip().casefold() == resolved.casefold():
                    coll_uid = str(row.get("Collection UID") or "").strip()
                    break
        if not coll_uid:
            info(f"Collection '{resolved}' not found — Collect to refresh collections.")
            return

        info(f"Removing {len(uids):,} model(s) from '{resolved}' on Sketchfab…")

        def _work():
            err = None
            ok = 0
            try:
                client = SketchfabClient()
                # Batch in chunks of 16
                chunk = 16
                for i in range(0, len(uids), chunk):
                    batch = [str(u).strip() for u in uids[i : i + chunk] if str(u).strip()]
                    if not batch:
                        continue
                    client.remove_models_from_collection(coll_uid, batch)
                    ok += len(batch)
                strip_already_in_inplace(state.liked_df, uids, resolved)
                # Also clear pending assign to that collection if present
                clear_assignment_inplace(state.liked_df, uids, collection=resolved)
                _persist_workbook()
            except Exception as exc:
                err = exc
                logger.exception("Remove from collection failed")

            async def _finish():
                liked_tab.bind_source(state.liked_df)
                for u in uids:
                    try:
                        liked_tab.apply_already_in_instant(u)
                        liked_tab.apply_cancel_instant(u, resolved)
                    except Exception:
                        pass
                pending_tab.mark_dirty(state.liked_df)
                refresh_counts()
                if err:
                    info(f"Remove from '{resolved}' failed: {err}")
                else:
                    info(f"Removed {ok:,} model(s) from '{resolved}' on Sketchfab.")
                page.update()

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_bulk_assign(uids: list[str], collection: str):
        from assign_actions import assign_models_inplace
        from collection_match import match_collection_name
        from push_assignments import pending_push_rows
        from search_filter import list_collection_names

        names = list_collection_names(state.colls_df, limit=50_000)
        resolved = match_collection_name(collection, names) or (collection or "").strip()
        n = assign_models_inplace(state.liked_df, uids, resolved, mode="add")
        if not n:
            info("No matching models to assign.")
            return
        # In-place orange "assigned" flare on rendered cards — no full grid reload.
        for u in uids:
            try:
                liked_tab.apply_assign_instant(u, resolved)
            except Exception:
                pass
        liked_tab.bind_source(state.liked_df)
        pending_tab.mark_dirty(state.liked_df)
        try:
            auto_assign_tab.set_df(state.liked_df)
        except Exception:
            pass
        refresh_counts()
        uid_set = {str(u).strip() for u in uids}
        new_pending = sum(
            1
            for r in pending_push_rows(state.liked_df)
            if r["collection"].casefold() == resolved.casefold() and r["uid"] in uid_set
        )
        pending_now = len({r["uid"] for r in pending_push_rows(state.liked_df)})

        def _bg():
            _persist_workbook()
            note = f"Assigned {n:,} model(s) → '{resolved}'"
            if resolved.casefold() != (collection or "").strip().casefold():
                note += f" (matched from '{collection}')"
            if new_pending:
                note += f" — {new_pending:,} added to Pending Push (Unpushed {pending_now:,})."
            else:
                note += " — already in that collection on the workbook (nothing new to Push)."
            info(note, quiet=True)
            log.flush()

        threading.Thread(target=_bg, daemon=True).start()
        _schedule_auto_push()

    def do_dismiss_suggestion(uid: str, collection: str):
        liked_tab.set_df(
            state.liked_df, state.liked_page, state.liked_page_size, state.colls_df,
            reset_scroll=False, refresh_dropdowns=False,
        )
        info(f"Hidden suggestion '{collection}' on this model only.")

    def do_quick_assign(letter: str):
        from quick_assign import resolve_collection
        from search_filter import list_collection_names

        uids = liked_tab.get_selected_uids()
        if not uids:
            info("Check models on Liked tab first (☑ per row or Page checkbox).")
            return
        coll = resolve_collection(letter, list_collection_names(state.colls_df))
        if not coll:
            info(f"No collection for '{letter}' — edit data/quick_assign.yaml")
            return
        state.liked_df = assign_models(state.liked_df, uids, coll)
        if not _persist_workbook():
            return
        liked_tab.clear_selection()
        liked_tab.bind_source(state.liked_df)
        for u in uids:
            liked_tab.apply_assign_instant(u, coll)
        pending_tab.set_df(state.liked_df)
        info(f"Assigned {len(uids):,} selected model(s) → '{coll}' (local — Push to sync).")
        _schedule_auto_push()

    def do_mark_unlisted(uids: list[str], extras: list[str] | None = None):
        from push_assignments import mark_unlisted_inplace

        n = mark_unlisted_inplace(state.liked_df, uids, extra_collections=extras or None)
        if not n:
            info("Nothing to mark Unlisted.")
            return
        if not _persist_workbook():
            return
        liked_tab.bind_source(state.liked_df)
        for u in uids:
            try:
                liked_tab.apply_already_in_instant(u)
            except Exception:
                pass
        pending_tab.mark_dirty(state.liked_df)
        refresh_counts()
        info(f"Marked {n:,} model(s) Unlisted (private shelf — like kept).")

    liked_tab.set_assign_callbacks(
        do_assign_model,
        do_bulk_assign,
        do_dismiss_suggestion,
        on_assign_create=do_assign_or_create,
        on_remove_from_collection=do_remove_from_collection,
        on_mark_unlisted=do_mark_unlisted,
    )
    def _update_pending_sync():
        try:
            from sync_status import local_sync_summary

            text, warn = local_sync_summary(state.liked_df)
            pending_tab.set_sync_info(text, warn=warn)
        except Exception:
            pass

    def do_verify_sync():
        if state.busy:
            info("Busy right now — try Check sync again in a moment.")
            return
        state.busy = True
        job_cancel.clear()

        def _work():
            from push_assignments import push
            from sync_status import local_sync_summary

            try:
                text, warn = local_sync_summary(state.liked_df)
                pending_tab.set_sync_info(text, warn=warn)
                progress_start("verify", "Push sync check", 0)
                res = push(
                    state.liked_df.copy(),
                    state.colls_df.copy(),
                    client=SketchfabClient.for_push(),
                    dry_run=True,
                    verify_membership=True,
                    on_progress=lambda d, t, m: progress_update("verify", d, t, m),
                    should_cancel=job_cancel.is_set,
                )
                if res.get("cancelled"):
                    progress_done("verify", "Push sync check stopped.")
                    return
                planned = int(res.get("planned") or 0)
                already = int(res.get("already_present") or 0)
                unknown = res.get("unknown_collections") or []
                skipped = int(res.get("skipped") or 0)
                local_models, local_pairs = 0, 0
                try:
                    from sync_status import local_pending_counts

                    local_models, local_pairs = local_pending_counts(state.liked_df)
                except Exception:
                    pass
                msg = (
                    f"Push sync check — {text}. "
                    f"Local Pending Push: {local_models:,} model(s) / {local_pairs:,} assignment(s). "
                    f"Sketchfab dry-run: {planned:,} still need posting, {already:,} already on site"
                )
                if skipped:
                    msg += f", {skipped:,} skipped"
                msg += "."
                if unknown:
                    msg += f" Unknown collection name(s): {', '.join(unknown[:5])}."
                if local_pairs and planned == 0 and already >= local_pairs:
                    msg += " Local queue matches Sketchfab — Push would be a no-op (or run Collect if Already In is stale)."
                elif local_pairs and planned:
                    msg += " Turn off Dry-run and Push to post the remaining assignments."
                elif not local_pairs and planned:
                    msg += " Workbook queue empty but Sketchfab still needs posts — Already In may be ahead of Assigned; check Liked filters."
                if warn:
                    msg += " Run Collect to pull website likes/collections."
                progress_done("verify", msg)
            except Exception as exc:
                logger.exception("Push sync check failed")
                try:
                    progress_done("verify", f"Push sync check failed: {exc}")
                except Exception:
                    pass
            finally:
                async def _finish():
                    state.busy = False
                    safe_page_update(page)

                page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    pending_tab.set_callbacks(
        do_cancel_pending,
        on_push=lambda: do_push(force_real=True),
        get_liked_df=lambda: state.liked_df,
        on_verify=do_verify_sync,
        on_cancel_all=do_cancel_all_pending,
    )

    def do_preview(
        uid: str,
        name: str = "",
        viewer_url: str = "",
        thumb_url: str = "",
        *,
        rect: dict | None = None,
    ) -> None:
        """Card 3D / hover → Chrome/Edge app sized over the exposed thumb image."""
        if not uid:
            return
        set_model(uid, name or uid, thumb_url or "")
        start_server()
        overlay = bool(rect and all(k in rect for k in ("left", "top", "width", "height")))
        url = build_viewer_url(uid, name or uid, thumb_url or "", overlay=overlay)
        if overlay:
            left = int(rect["left"])
            top = int(rect["top"])
            width = int(rect["width"])
            height = int(rect["height"])
        else:
            from ui.viewer_place import screen_rect_from_cursor

            fallback = screen_rect_from_cursor(220, 200)
            if fallback:
                left = fallback["left"]
                top = fallback["top"]
                width = fallback["width"]
                height = fallback["height"]
            else:
                left = top = None
                width, height = 240, 260
                try:
                    left = int(getattr(page.window, "left", None) or 80) + 80
                    top = int(getattr(page.window, "top", None) or 80) + 120
                except Exception:
                    left, top = None, None
        ok = open_viewer_app(url, width=width, height=height, left=left, top=top)
        info(
            f"3D overlay {width}×{height} @ {left},{top}: {name or uid}"
            if ok
            else f"Could not open 3D viewer for {name or uid}",
            quiet=True,
        )

    def do_download(uid: str, name: str) -> None:
        if not uid:
            return
        dirs = ensure_download_dirs()
        if not dirs:
            info("Could not create download folder — check SKETCHFAB_DOWNLOAD_ROOT in .env (e.g. F:\\)")
            return
        root = pathlib.Path(dirs[0])
        already = assigned = ""
        author = license_ = ""
        in_likes = False
        marked_dl = False
        uc = uid_col(state.liked_df) if not state.liked_df.empty else ""
        if uc:
            hit = state.liked_df[state.liked_df[uc].astype(str) == str(uid)]
            if not hit.empty:
                in_likes = True
                row = hit.iloc[0]
                already = row.get("Already In Collection(s)") or ""
                assigned = row.get("Assigned Collection(s)") or ""
                author = str(row.get("Author") or "").strip()
                license_ = str(row.get("License") or "").strip()
                marked_dl = str(row.get("Downloadable") or "").strip().lower() in {"yes", "y", "true", "1"}
                if author.casefold() in {"nan", "none", "<na>"}:
                    author = ""
                if license_.casefold() in {"nan", "none", "<na>"}:
                    license_ = ""
                if not name:
                    name = str(row.get("Name") or row.get("Model Name") or uid)
        if not marked_dl:
            info(f"Trying Download API for {name or uid} (not marked downloadable)…")
        from not_liked import NOTLIKED_FOLDER

        colls = collections_for_download(already, assigned)
        if not colls:
            colls = [NOTLIKED_FOLDER] if not in_likes else ["_Unsorted"]
        primary = colls[0]
        dest = resolve_model_dir(
            root / safe_collection_folder(primary),
            name=name or uid,
            author=author,
            license=license_,
            uid=uid,
        )
        link_dirs = [
            resolve_model_dir(
                root / safe_collection_folder(c),
                name=name or uid,
                author=author,
                license=license_,
                uid=uid,
            )
            for c in colls[1:]
        ]
        dl_queue.configure(download_fn=download_glb, dest_dir=root, on_complete=_on_download_complete)
        if not dl_queue.enqueue(
            uid,
            name or uid,
            dest_dir=dest,
            link_dirs=link_dirs,
            author=author,
            license=license_,
        ):
            info(f"Already queued / downloading: {name or uid}", quiet=True)
            return
        where = primary if len(colls) == 1 else f"{primary} (+{len(colls) - 1} link)"
        note = f"Queued download → {where}/{dest.name}: {name or uid}"
        if primary == NOTLIKED_FOLDER:
            note += " (NotLiked — not in your likes yet)"
        info(note, quiet=True)
        log.flush()

    def _on_download_complete(uid: str, name: str, path, err: str | None) -> None:
        if err:
            async def _fail():
                info(f"Download failed ({name or uid}): {err}")

            page.run_task(_fail)
            return

        # Decide Liked vs NotLiked before UI/background work so both share the same flag.
        in_likes = False
        uc = uid_col(state.liked_df) if not state.liked_df.empty else ""
        if uc:
            hit = state.liked_df[state.liked_df[uc].astype(str) == str(uid)]
            in_likes = not hit.empty

        async def _ui():
            # Instant green icon — no disk walk, no grid remount, no xlsx on this loop tick.
            if in_likes:
                mark_downloaded_inplace(state.liked_df, uid, path or "")
                liked_tab.bind_source(state.liked_df)
                liked_tab.paint_downloaded(uid, on_disk=True)
            else:
                from not_liked import record_download

                record_download(uid, name=name or "", path=str(path or ""))
                try:
                    account_tab.paint_downloaded(uid, on_disk=True)
                except Exception:
                    pass
                try:
                    browse_tab.paint_downloaded(uid, on_disk=True)
                except Exception:
                    pass
            try:
                if liked_tab._lod_debouncer:
                    liked_tab._lod_debouncer.kick(
                        page,
                        scroll=getattr(liked_tab, "_scroll_offset", 0.0) or 0.0,
                        viewport=getattr(liked_tab, "_viewport_h", 800.0) or 800.0,
                    )
            except Exception:
                pass
            info(f"Downloaded → {path}" + ("" if in_likes else " (NotLiked)"))
            page.update()

        page.run_task(_ui)

        def _bg_persist():
            try:
                if in_likes and _persist_workbook():
                    async def _counts():
                        refresh_counts()

                    page.run_task(_counts)
            except Exception:
                logger.exception("Background workbook save after download failed")

        threading.Thread(target=_bg_persist, daemon=True).start()

    def do_organize_downloads():
        """Move existing GLBs into collection folders (only folders that have downloads)."""
        if getattr(dl_queue, "_organizing", False):
            return
        dirs = ensure_download_dirs()
        if not dirs:
            info("Could not create download folder — check SKETCHFAB_DOWNLOAD_ROOT in .env")
            return
        dl_queue.set_organizing(True)

        def _work():
            err = None
            stats = {}
            liked_refresh = None
            try:
                stats = organize_downloads_by_collection(state.liked_df, dirs)
                try:
                    liked_refresh = _noneify(enrich_download_status(state.liked_df, dirs))
                except Exception:
                    logger.exception("Post-organize enrich failed")
            except Exception as exc:
                err = exc
                logger.exception("Organize downloads failed")

            async def _finish():
                try:
                    if liked_refresh is not None:
                        state.liked_df = liked_refresh
                        liked_tab.bind_source(state.liked_df)
                    n_folders = len(stats.get("folders") or [])
                    if err:
                        info(f"Organize failed: {err}")
                        dl_queue.set_organizing(False, "Organize failed")
                    else:
                        unsorted_n = int(stats.get("unsorted") or 0)
                        unsorted_bit = f", unsorted {unsorted_n:,}" if unsorted_n else ""
                        msg = (
                            f"Organized {stats.get('files', 0):,} file(s) → "
                            f"{n_folders:,} collection folder(s) "
                            f"(moved {stats.get('moved', 0):,}, "
                            f"linked {stats.get('linked', 0):,}, "
                            f"already ok {stats.get('already_ok', 0):,}"
                            f"{unsorted_bit})"
                        )
                        errs = stats.get("errors") or []
                        if errs:
                            msg += f" · {len(errs)} error(s)"
                        dl_queue.set_organizing(False, f"{n_folders} folders")
                        info(msg)
                        for e in errs[:5]:
                            info(f"  {e}")
                        refresh_counts()
                finally:
                    # Never leave Organize stuck in busy state.
                    if getattr(dl_queue, "_organizing", False):
                        dl_queue.set_organizing(False, "Idle")
                page.update()

            page.run_task(_finish)

            # Persist after UI unlock — Excel lock used to leave Organize spinning.
            if liked_refresh is not None and err is None:
                try:
                    _persist_workbook(liked=liked_refresh)
                except Exception:
                    logger.exception("Post-organize workbook update failed")

        threading.Thread(target=_work, daemon=True).start()

    dl_queue.set_organize_callback(do_organize_downloads)
    liked_tab.set_model_callbacks(do_preview, do_download, on_open_account=go_to_account)

    def do_series_like(seed_names: list[str], seed_authors: list[str]) -> None:
        from series_like import run_series_like
        from state import name_col

        df = state.liked_df
        nc = name_col(df) if not df.empty else "Name"
        all_names = (
            df[nc].fillna("").astype(str).tolist()
            if (not df.empty and nc in df.columns)
            else []
        )
        liked = {u.casefold() for u in _liked_uids()}
        info(
            f"Series discover: {len(seed_names)} seed name(s), T0–T10 + family from likes… "
            f"(author hint: {seed_authors[0] if seed_authors else 'none'})"
        )

        def _work():
            try:
                res = run_series_like(
                    SketchfabClient(),
                    seed_names=seed_names,
                    seed_authors=seed_authors,
                    all_liked_names=all_names,
                    liked_uids=liked,
                    on_progress=lambda i, t, m: info(m, quiet=(i % 8 != 0)),
                )
            except Exception as exc:
                err_msg = str(exc)

                async def _err():
                    info(f"Series discover failed: {err_msg}")

                page.run_task(_err)
                return

            async def _done():
                msg = (
                    f"Series discover: {res.liked} liked · {res.already_liked} already · "
                    f"{res.not_found} not found · {res.found} matched"
                )
                if res.errors:
                    msg += f" · {len(res.errors)} errors"
                msg += " — Collect to sync workbook."
                info(msg)
                for uid in res.newly_liked_uids:
                    try:
                        from not_liked import mark_liked as mark_not_liked_liked

                        mark_not_liked_liked(uid)
                    except Exception:
                        pass

            page.run_task(_done)

        threading.Thread(target=_work, daemon=True).start()

    liked_tab.set_series_like_callback(do_series_like)

    def _subscribed_uids() -> set[str]:
        df = state.subs_df
        if df is None or getattr(df, "empty", True):
            return set()
        col = "Collection UID" if "Collection UID" in df.columns else ""
        if not col:
            return set()
        return {str(u).strip() for u in df[col].tolist() if str(u).strip()}

    def _sync_collection_tabs_subs() -> None:
        uids = _subscribed_uids()
        try:
            colls_tab.set_subscribed_uids(uids)
        except Exception:
            pass
        try:
            browse_colls_tab.set_subscribed_uids(uids)
        except Exception:
            pass

    def apply_subscribe_collection(meta: dict, want: bool) -> None:
        """API + workbook — safe to call from a worker thread (no page.run_task)."""
        uid = str((meta or {}).get("UID") or "").strip()
        if not uid:
            raise ValueError("Collection UID required")
        client = SketchfabClient()
        if want:
            try:
                client.subscribe_collection(uid)
            except Exception as exc:
                low = str(exc).casefold()
                if uid in _subscribed_uids() or ("400" in str(exc) and "subscri" in low):
                    pass
                else:
                    raise
            if uid not in _subscribed_uids():
                row = {
                    "Collection Name": (meta or {}).get("Name") or "",
                    "Collection UID": uid,
                    "Owner": (meta or {}).get("Author") or "",
                    "Owner Profile": (meta or {}).get("Author Profile") or "",
                    "Model Count": (meta or {}).get("Model Count")
                    if (meta or {}).get("Model Count") is not None
                    else "",
                }
                state.subs_df = pd.concat([state.subs_df, pd.DataFrame([row])], ignore_index=True)
                state.subs_df = _noneify(state.subs_df)
        else:
            client.unsubscribe_collection(uid)
            if not state.subs_df.empty and "Collection UID" in state.subs_df.columns:
                state.subs_df = _noneify(
                    state.subs_df[state.subs_df["Collection UID"].astype(str) != uid].reset_index(drop=True)
                )
        try:
            write_workbook(state.liked_df, state.colls_df, state.subs_df)
        except Exception:
            pass

    def notify_subs_changed() -> None:
        async def _ui():
            _sync_collection_tabs_subs()
            subs_tab.set_df(state.subs_df, state.subs_page, state.subs_page_size)
            apply_pagers()
            refresh_counts()
            try:
                page.update()
            except Exception:
                pass

        page.run_task(_ui)

    def do_subscribe_collection(meta: dict, want: bool) -> None:
        """Subscribe/unsubscribe (UI thread — updates Subscribed tab)."""
        apply_subscribe_collection(meta, want)
        notify_subs_changed()

    colls_tab.set_page(page)
    colls_tab.set_client_factory(lambda: SketchfabClient())
    colls_tab.set_model_callbacks(do_preview, do_download, on_open_account=go_to_account)
    colls_tab.set_subscribe_callback(do_subscribe_collection)

    def do_remove_from_open_collection(model_uid: str, coll_uid: str, meta: dict) -> None:
        """Called from My Collections card X — runs on a worker thread; raises on failure."""
        model_uid = str(model_uid or "").strip()
        coll_uid = str(coll_uid or "").strip()
        name = str((meta or {}).get("Name") or "").strip()
        if not model_uid or not coll_uid:
            raise ValueError("Model and collection required")
        SketchfabClient().remove_model_from_collection(coll_uid, model_uid)
        if name:
            strip_already_in_inplace(state.liked_df, [model_uid], name)
            clear_assignment_inplace(state.liked_df, [model_uid], collection=name)
            try:
                _persist_workbook()
            except Exception:
                pass

            async def _ui():
                liked_tab.bind_source(state.liked_df)
                try:
                    liked_tab.apply_already_in_instant(model_uid)
                    liked_tab.apply_cancel_instant(model_uid, name)
                except Exception:
                    pass
                pending_tab.mark_dirty(state.liked_df)
                refresh_counts()
                page.update()

            page.run_task(_ui)

    colls_tab.set_remove_callback(do_remove_from_open_collection)
    browse_colls_tab.set_page(page)
    browse_colls_tab.set_client_factory(lambda: SketchfabClient())
    browse_colls_tab.set_model_callbacks(do_preview, do_download, on_open_account=go_to_account)
    browse_colls_tab.set_subscribe_callback(apply_subscribe_collection)
    browse_colls_tab.set_subscribe_ui_hook(notify_subs_changed)
    # Liked owns Space → autoscroll; re-bind after other tabs' set_page.
    liked_tab.set_page(page)
    _sync_collection_tabs_subs()

    _detail_cache: dict[str, tuple[float, dict]] = {}
    _DETAIL_CACHE_TTL = 300.0

    def fetch_selection_detail(uid: str) -> dict:
        uid = str(uid or "").strip()
        if not uid:
            return {}
        now = time.time()
        cached = _detail_cache.get(uid)
        if cached and (now - cached[0]) < _DETAIL_CACHE_TTL:
            return cached[1]
        m = SketchfabClient().get_model(uid)
        user = m.get("user") or {}
        lic = m.get("license") or {}
        tags = m.get("tags") or []
        cats = m.get("categories") or []
        tag_names = []
        for t in tags:
            if isinstance(t, dict):
                tag_names.append(str(t.get("name") or t.get("slug") or ""))
            else:
                tag_names.append(str(t))
        cat_names = []
        for c in cats:
            if isinstance(c, dict):
                cat_names.append(str(c.get("name") or c.get("slug") or ""))
            else:
                cat_names.append(str(c))
        out = {
            "name": m.get("name") or "",
            "description": m.get("description") or "",
            "author": user.get("displayName") or user.get("username") or "",
            "license": lic.get("label") or lic.get("slug") or "",
            "faceCount": m.get("faceCount"),
            "viewCount": m.get("viewCount"),
            "likeCount": m.get("likeCount"),
            "downloadCount": m.get("downloadCount"),
            "tags": ", ".join(x for x in tag_names if x),
            "categories": ", ".join(x for x in cat_names if x),
            "url": m.get("viewerUrl") or m.get("uri") or f"https://sketchfab.com/3d-models/{uid}",
        }
        _detail_cache[uid] = (now, out)
        if len(_detail_cache) > 200:
            oldest = min(_detail_cache, key=lambda k: _detail_cache[k][0])
            _detail_cache.pop(oldest, None)
        return out

    detail_pane.set_fetch_callback(fetch_selection_detail)

    def on_selection(uids: list[str], row: dict | None) -> None:
        detail_pane.show_selection(uids, row=row)
        if len(uids) == 1 and row:
            uid = str(uids[0] or row.get("UID") or "").strip()
            name = str(row.get("Name") or row.get("Model Name") or uid).strip()
            if uid:
                inspector_tab.set_model(uid, name)

    liked_tab.set_selection_callback(on_selection)

    def do_account_user(key: str) -> dict:
        return SketchfabClient().get_user(key)

    def do_search_users(
        query: str = "",
        sort_by: str = "",
        cursor_url: str | None = None,
    ) -> dict:
        return SketchfabClient().search_users(
            query=query,
            sort_by=sort_by,
            cursor_url=cursor_url,
        )

    def do_user_models_preview(username: str, count: int = 3) -> list[dict]:
        from browse_models import normalize_search_model

        data = SketchfabClient().list_user_models(username, count=count)
        return [normalize_search_model(m) for m in data.get("results") or []]

    def do_account_section(section: str, username: str, cursor_url: str | None):
        client = SketchfabClient()
        if section == "models":
            return client.list_user_models(username, cursor_url=cursor_url)
        if section == "likes":
            return client.list_user_likes(username, cursor_url=cursor_url)
        if section == "collections":
            return client.list_user_collections(username, cursor_url=cursor_url)
        if section == "subs":
            return client.list_my_subscriptions(cursor_url=cursor_url)
        raise ValueError(f"Unknown account section: {section}")

    def do_export_browse(rows: list[dict], query: str, session: bool = False) -> None:
        try:
            path, n = append_browse_results(rows, query=query)
            label = "session" if session else "page"
            info(f"Exported {n} browse row(s) ({label}) → {path} [Browse Results sheet]")
        except Exception as e:
            info(f"Export failed: {e}")

    def do_save_preset(preset: dict) -> None:
        try:
            presets = load_presets()
            name = preset.get("name", "")
            presets = [p for p in presets if p.get("name") != name]
            presets.insert(0, preset)
            save_presets(presets)
            browse_tab.on_presets_changed()
            info(f"Saved browse preset '{name}'.")
        except Exception as e:
            info(f"Preset save failed: {e}")

    def do_delete_preset(name: str) -> None:
        try:
            presets = [p for p in load_presets() if p.get("name") != name]
            save_presets(presets)
            browse_tab.on_presets_changed()
            info(f"Deleted preset '{name}'.")
        except Exception as e:
            info(f"Preset delete failed: {e}")

    def _liked_uids() -> set[str]:
        if state.liked_df.empty or "UID" not in state.liked_df.columns:
            return set()
        return {
            u for u in (
                str(x).strip().lower()
                for x in state.liked_df["UID"].fillna("").astype(str)
            )
            if u
        }

    def _downloaded_uids() -> set[str]:
        out: set[str] = set()
        try:
            from not_liked import tracked_uids

            out |= {u.lower() for u in tracked_uids()}
        except Exception:
            pass
        if state.liked_df.empty or "UID" not in state.liked_df.columns:
            return out
        if "Downloaded" not in state.liked_df.columns:
            return out
        uc = "UID"
        for _, row in state.liked_df.iterrows():
            if str(row.get("Downloaded") or "").strip().lower() in {"yes", "y", "true", "1"}:
                u = str(row.get(uc) or "").strip().lower()
                if u:
                    out.add(u)
        return out

    def _schedule_browse_prefetch() -> None:
        url = browse_tab._next_url
        if not url:
            return
        if browse_tab._prefetch_url == url and browse_tab._prefetch_data:
            return
        if browse_tab._prefetch_inflight_url == url:
            return
        browse_tab._prefetch_inflight_url = url

        def _work():
            try:
                data = SketchfabClient().search_models(cursor_url=url)

                async def _done():
                    browse_tab._prefetch_inflight_url = None
                    if browse_tab._next_url == url:
                        browse_tab._prefetch_url = url
                        browse_tab._prefetch_data = data

                page.run_task(_done)
            except Exception:
                logger.debug("Browse prefetch failed", exc_info=True)

                async def _fail():
                    browse_tab._prefetch_inflight_url = None

                page.run_task(_fail)

        threading.Thread(target=_work, daemon=True).start()

    def _finish_browse_page(data: dict, *, append: bool, fresh: bool = False) -> None:
        try:
            browse_tab.set_liked_uids(_liked_uids())
            browse_tab.set_api_page(data, append=append)
            shown = len(browse_tab._filter_rows())
            loaded = len(browse_tab._rows)
            more = " · scroll for more" if browse_tab._next_url else ""
            info(f"Browse: {shown:,} shown · {loaded:,} loaded{more}")
            _schedule_browse_prefetch()
        except Exception as exc:
            logger.exception("Browse UI update failed")
            info(f"Browse data loaded but grid refresh failed: {exc}")
        finally:
            browse_tab._fetch_inflight_url = None
            if not append:
                browse_tab.set_busy(False)

    def do_browse(cursor_url: str | None = None, append: bool = False):
        # Browse uses its own busy flag — Collect/Push can run in parallel.
        fresh = cursor_url is None and not append
        if fresh:
            browse_tab.clear_prefetch()

        if append and cursor_url:
            if browse_tab._prefetch_url == cursor_url and browse_tab._prefetch_data:
                data = browse_tab._prefetch_data
                browse_tab._prefetch_url = None
                browse_tab._prefetch_data = None
                browse_tab._fetch_inflight_url = cursor_url
                _finish_browse_page(data, append=True)
                return
            if browse_tab._fetch_inflight_url:
                return

        elif browse_tab._busy:
            return

        if append and cursor_url:
            browse_tab._fetch_inflight_url = cursor_url
            browse_tab.arm_fetch_timeout(cursor_url)
        elif fresh:
            browse_tab.set_busy(True)

        params = browse_tab.search_params()
        browse_tab._last_search_params = dict(params)

        def _work():
            err = None
            data = None
            try:
                client = SketchfabClient()
                browse_kw = {"max_retries": 4, "retry_wait_cap": 18.0}
                if cursor_url:
                    data = client.search_models(cursor_url=cursor_url, **browse_kw)
                else:
                    data = client.search_models(**params, **browse_kw)
            except Exception as e:
                err = str(e)
                logger.exception("Browse search failed")

            async def _finish():
                if err:
                    browse_tab._fetch_inflight_url = None
                    if not append:
                        browse_tab.set_busy(False)
                        short = err if len(err) < 160 else err[:157] + "…"
                        browse_tab._show_placeholder(f"Search failed — {short}")
                        browse_tab._safe_update(browse_tab.scroller, browse_tab._status)
                    info(f"Browse failed: {err}")
                elif data:
                    _finish_browse_page(data, append=append, fresh=fresh)

            try:
                page.run_task(_finish)
            except Exception:
                browse_tab._fetch_inflight_url = None
                if not append:
                    browse_tab.set_busy(False)

        threading.Thread(target=_work, daemon=True).start()

    def do_browse_estimate():
        from browse_search import estimate_api_matches

        params = dict(browse_tab._last_search_params or {})
        date_key = browse_tab._date_filter_key

        def _work():
            count = -1
            truncated = False
            try:
                count, truncated = estimate_api_matches(SketchfabClient(), params, date_key)
            except Exception:
                count = -1

            async def _finish():
                if count >= 0:
                    browse_tab.set_match_estimate(count, truncated=truncated)
                    suffix = "+" if truncated else ""
                    info(f"Browse estimate: ~{count:,}{suffix} models match API filters.")

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_browse_enrich(uids: list[str]):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        batch = [u for u in uids if u][:36]
        if not batch:
            return

        def _work():
            client = SketchfabClient()
            counts: dict[str, int] = {}

            def _one(uid: str) -> tuple[str, int | None]:
                try:
                    m = client.get_model(uid)
                    return uid, int(m.get("downloadCount") or 0)
                except Exception:
                    return uid, None

            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(_one, uid) for uid in batch]
                for fut in as_completed(futures):
                    uid, dc = fut.result()
                    if dc is not None:
                        counts[uid] = dc

            async def _finish():
                browse_tab.patch_download_counts(counts)

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_browse_toggle_like(uid: str, *, unlike: bool = False):
        if not uid:
            return
        if unlike:
            do_browse_unlike(uid)
        else:
            do_browse_like(uid)

    def do_browse_like(uid: str):
        uid = str(uid or "").strip().lower()
        if not uid:
            return
        # Optimistic UI — update hearts immediately without a full page.refresh.
        try:
            browse_tab.mark_liked(uid)
        except Exception:
            pass
        try:
            account_tab.mark_liked(uid)
        except Exception:
            pass
        try:
            browse_colls_tab.mark_liked(uid)
            colls_tab.mark_liked(uid)
        except Exception:
            pass
        try:
            from not_liked import mark_liked as mark_not_liked_liked

            mark_not_liked_liked(uid)
        except Exception:
            pass

        def _work():
            err = None
            try:
                SketchfabClient().like_model(uid)
            except Exception as e:
                err = str(e)

            async def _finish():
                if err:
                    already = "400" in err and "likes" in err.lower()
                    if already:
                        try:
                            browse_tab.mark_liked(uid)
                            account_tab.mark_liked(uid)
                        except Exception:
                            pass
                        info(f"Already liked on Sketchfab ({uid}) — marked locally.")
                    else:
                        try:
                            browse_tab.revert_optimistic_like(uid)
                        except Exception:
                            pass
                        hint = err
                        if "500" in err:
                            hint = (
                                f"{err} — Sketchfab server glitch; heart cleared. "
                                "Wait a few seconds and click again."
                            )
                        info(f"Like failed: {hint}")
                else:
                    info(f"Liked model {uid} on Sketchfab — Collect to sync workbook.")
                # Do NOT page.update() — that remounts lists and lags while liking.

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_browse_unlike(uid: str):
        uid = str(uid or "").strip().lower()
        if not uid:
            return
        try:
            browse_tab.mark_unliked(uid)
        except Exception:
            pass
        try:
            account_tab.mark_unliked(uid)
        except Exception:
            pass

        def _work():
            err = None
            try:
                SketchfabClient().unlike_model(uid)
            except Exception as e:
                err = str(e)

            async def _finish():
                if err:
                    gone = "404" in err and (
                        "not found" in err.lower() or "likes" in err.lower()
                    )
                    if gone:
                        info(
                            f"Not in your Sketchfab likes ({uid}) — "
                            f"cleared locally (Collect to sync workbook)."
                        )
                    else:
                        info(f"Unlike failed: {err}")
                        try:
                            browse_tab.mark_liked(uid)
                            account_tab.mark_liked(uid)
                        except Exception:
                            pass
                else:
                    info(f"Unliked model {uid} on Sketchfab — Collect to sync workbook.")

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_account_follow(action: str, user_uid: str):
        client = SketchfabClient()
        if action == "status":
            return client.is_following_user(user_uid)
        if action == "follow":
            client.follow_user(user_uid)
            return True
        if action == "unfollow":
            client.unfollow_user(user_uid)
            return True
        raise ValueError(f"Unknown follow action: {action}")

    browse_tab.set_callbacks(
        do_browse,
        do_browse,
        do_browse_toggle_like,
        on_preview=do_preview,
        on_download=do_download,
        on_export=do_export_browse,
        on_save_preset=do_save_preset,
        on_delete_preset=do_delete_preset,
        on_enrich=do_browse_enrich,
        on_open_account=go_to_account,
    )
    browse_tab.set_selection_callback(on_selection)
    account_tab.set_callbacks(
        on_load_user=do_account_user,
        on_load_section=do_account_section,
        on_preview=do_preview,
        on_download=do_download,
        on_like=do_browse_toggle_like,
        on_open_account=go_to_account,
        on_follow=do_account_follow,
        on_open_my_collection=go_to_my_collection,
        on_open_browse_collection=go_to_browse_collection,
        get_client=lambda: SketchfabClient(),
        on_search_users=do_search_users,
        on_user_models_preview=do_user_models_preview,
    )
    try:
        account_tab.set_liked_uids(_liked_uids())
        account_tab.set_downloaded_uids(_downloaded_uids())
    except Exception:
        pass
    try:
        browse_tab.set_downloaded_uids(_downloaded_uids())
    except Exception:
        pass
    def do_author_collection_scan(author: str) -> None:
        from collection_author_scan import scan_collections_for_author
        from state import name_col

        author = (author or "").strip()
        if not author:
            return
        extra_uids: list[str] = []
        extra_names: list[str] = []
        df = state.colls_df
        if df is not None and not df.empty:
            col = "Collection UID" if "Collection UID" in df.columns else ""
            if col:
                extra_uids = [
                    str(u).strip()
                    for u in df[col].tolist()
                    if str(u).strip()
                ]
        liked = state.liked_df
        if liked is not None and not liked.empty:
            nc = name_col(liked) if "Name" in liked.columns or "Model Name" in liked.columns else ""
            ac = "Author" if "Author" in liked.columns else ""
            if nc:
                for _, row in liked.iterrows():
                    a = str(row.get(ac) or "").strip() if ac else ""
                    n = str(row.get(nc) or "").strip()
                    if not n or "_" not in n:
                        continue
                    if a and author.casefold() not in a.casefold() and a.casefold() not in author.casefold():
                        continue
                    extra_names.append(n)
        info(f"Scanning public + subscribed collections for author “{author}”…")

        def _work():
            try:
                res = scan_collections_for_author(
                    SketchfabClient(),
                    author,
                    extra_collection_uids=extra_uids,
                    extra_family_names=extra_names,
                    author_aliases_extra=["Artist"] if author.casefold() != "Artist" else None,
                    on_progress=lambda i, t, m: info(m, quiet=(i % 6 != 0)),
                )
            except Exception as exc:
                err_msg = str(exc)

                async def _err():
                    info(f"Collection author scan failed: {err_msg}")

                page.run_task(_err)
                return

            async def _done():
                msg = (
                    f"Collection scan ({author}): {res.models_found} model(s) in "
                    f"{len(res.collection_hits)} collection(s) "
                    f"(scanned {res.collections_scanned})"
                )
                if res.title_variants_added:
                    msg += f" · +{res.title_variants_added} from title variants ({res.title_variants_searched} patterns, broad search)"
                if res.errors:
                    msg += f" · {len(res.errors)} collection errors"
                info(msg)
                if res.models:
                    browse_colls_tab.show_author_scan_results(
                        author,
                        res.models,
                        collections_scanned=res.collections_scanned,
                    )
                    tabs_ctrl = page.tabs if hasattr(page, "tabs") else None
                    if tabs_ctrl is not None:
                        tabs_ctrl.selected_index = 7
                else:
                    info(f"No models by “{author}” found in scanned collections — try Liked author filter or Series discover.")

            page.run_task(_done)

        threading.Thread(target=_work, daemon=True).start()

    colls_tab.set_like_callback(do_browse_like)
    browse_colls_tab.set_like_callback(do_browse_like)
    browse_colls_tab.set_author_scan_callback(do_author_collection_scan)
    try:
        uids = _liked_uids()
        colls_tab.set_liked_uids(uids)
        browse_colls_tab.set_liked_uids(uids)
    except Exception:
        pass

    def _load_categories():
        def _work():
            cats = []
            err = None
            try:
                cats = SketchfabClient().get_categories()
            except Exception as e:
                err = str(e)

            async def _finish():
                if cats:
                    browse_tab.set_categories(cats)
                elif err:
                    info(f"Categories load skipped: {err}")

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_content_filter_change():
        state.hide_nsfw = hide_nsfw_ref.value
        state.hide_female = hide_female_ref.value
        state.hide_male = hide_male_ref.value
        flags = flags_from_state(state)
        broadcast_content_filters(
            flags,
            (
                liked_tab,
                browse_tab,
                colls_tab,
                browse_colls_tab,
                account_tab,
                pending_tab,
                subs_tab,
                report_tab,
            ),
        )
        refresh_tables()
        info(f"Content filters: {flags.label()} (view only — pending still pushes)")

    def _nwm_targets():
        return (
            liked_tab,
            browse_tab,
            colls_tab,
            browse_colls_tab,
            account_tab,
            pending_tab,
            subs_tab,
            report_tab,
        )

    def refresh_background_tabs():
        """Refresh non-Liked tabs without rebuilding the liked grid."""
        broadcast_content_filters(flags_from_state(state), _nwm_targets())
        try:
            browse_tab.set_liked_uids(_liked_uids())
        except Exception:
            pass
        try:
            uids = _liked_uids()
            colls_tab.set_liked_uids(uids)
            browse_colls_tab.set_liked_uids(uids)
            account_tab.set_liked_uids(uids)
            try:
                dls = _downloaded_uids()
                account_tab.set_downloaded_uids(dls)
                browse_tab.set_downloaded_uids(dls)
            except Exception:
                pass
        except Exception:
            pass
        try:
            colls_tab.set_liked_df(state.liked_df)
        except Exception:
            pass
        try:
            colls_tab.set_df(state.colls_df, state.colls_page, state.colls_page_size)
        except (AssertionError, RuntimeError, Exception):
            pass
        try:
            subs_tab.set_df(state.subs_df, state.subs_page, state.subs_page_size)
        except (AssertionError, RuntimeError, Exception):
            pass
        _sync_collection_tabs_subs()
        refresh_counts()
        apply_pagers()
        try:
            pairs = find_similar_collections(state.colls_df) if not state.colls_df.empty else []
        except Exception:
            pairs = []
        try:
            report_tab.set_data(
                state.liked_df, state.colls_df, pairs,
                hide_nsfw=state.hide_nsfw, hide_female=state.hide_female, hide_male=state.hide_male,
            )
        except Exception:
            pass
        try:
            pending_tab.set_df(state.liked_df)
        except Exception:
            pass
        try:
            auto_assign_tab.set_df(state.liked_df)
        except Exception:
            pass

    def refresh_tables():
        broadcast_content_filters(flags_from_state(state), _nwm_targets())
        try:
            liked_tab.set_df(
                state.liked_df,
                state.liked_page,
                state.liked_page_size,
                state.colls_df,
                reset_scroll=False,
            )
        except (AssertionError, RuntimeError, Exception):
            logger.exception("Liked tab refresh failed")
        refresh_background_tabs()

    def refresh_liked_soft():
        """Fast path after assign/dismiss — Liked + counts; keep scroll position."""
        liked_tab.set_content_filters(state.hide_nsfw, state.hide_female, state.hide_male)
        liked_tab.set_df(
            state.liked_df,
            state.liked_page,
            state.liked_page_size,
            state.colls_df,
            reset_scroll=False,
            refresh_dropdowns=False,
        )
        refresh_counts()
        page.update()

    def info(msg: str, *, quiet: bool = False):
        # LogView.append already updates itself — avoid page.update() so Liked
        # ListView keeps scroll position (full page updates jump to top).
        # quiet=True buffers text without syncing UI (assign path).
        log.append(msg, sync=not quiet)
        if msg:
            log_activity(msg)

    try:
        colls_tab.set_info_fn(lambda m: info(m, quiet=True))
        browse_colls_tab.set_info_fn(lambda m: info(m, quiet=True))
    except Exception:
        pass

    def progress_start(key: str, label: str, total: int = 0):
        log.progress_start(key, label, total)

    def progress_update(key: str, done: int, total: int | None = None, msg: str = ""):
        log.progress_update(key, done, total, msg)

    def progress_done(key: str, summary: str = ""):
        log.progress_done(key, summary)
        if summary:
            log_activity(summary)

    _auto_push_timer: threading.Timer | None = None
    _auto_push_lock = threading.Lock()

    def _schedule_auto_push():
        """Debounce Push after assigns when Auto-push is on."""
        nonlocal _auto_push_timer
        if not auto_push_ref.value:
            return
        with _auto_push_lock:
            if _auto_push_timer is not None:
                try:
                    _auto_push_timer.cancel()
                except Exception:
                    pass

            def _fire():
                if auto_push_ref.value and not state.busy:
                    if dry_run_ref.value:
                        info("Auto-push skipped — Dry-run is ON (turn off Dry-run or use Pending → Push now).", quiet=True)
                        return
                    info("Auto-push…")
                    do_push()

            _auto_push_timer = threading.Timer(1.6, _fire)
            _auto_push_timer.daemon = True
            _auto_push_timer.start()

    def _persist_workbook(
        liked=None,
        colls=None,
        subs=None,
        *,
        ok_msg: str | None = None,
    ) -> bool:
        try:
            path = write_workbook(
                state.liked_df if liked is None else liked,
                state.colls_df if colls is None else colls,
                state.subs_df if subs is None else subs,
            )
            if ok_msg:
                info(ok_msg.format(path=path) if "{path}" in ok_msg else ok_msg)
            return True
        except Exception as exc:
            logger.exception("Workbook write failed")
            info(
                f"Could not save workbook ({exc}). "
                "Close data/sketchfab_data.xlsx in Excel if it is open. "
                "Restore from data/sketchfab_data.xlsx.bak if the file looks corrupt."
            )
            return False

    def _load_username():
        try:
            me = SketchfabClient().get_me()
            un = (me.get("username") or "").strip()
            set_sketchfab_username(un)
            state.username = un
            account_tab.set_me_username(un)
            if un:
                pass  # username is used for collection URLs; no need to spam Activity
        except Exception:
            pass

    def _read_workbook_state(scan: bool = True, fetch_subs_if_empty: bool = True) -> None:
        """Load workbook into state only — safe from worker threads (no Flet updates)."""
        likes, colls, subs = read_workbook(scan_downloads=scan)
        if (
            fetch_subs_if_empty
            and subs.empty
            and os.path.exists(XL_PATH)
            and os.environ.get("SKETCHFAB_TOKEN")
        ):
            try:
                n = refresh_subscriptions_sheet(XL_PATH)
                likes, colls, subs = read_workbook(scan_downloads=scan)
                info(f"Fetched {n} subscribed collection(s).")
            except Exception as e:
                info(f"Subscription fetch skipped: {e}")
        state.liked_df = _noneify(normalize_liked_columns(likes))
        state.colls_df = _noneify(colls)
        state.subs_df = _noneify(subs)

    def _load_data(scan: bool = True, fetch_subs_if_empty: bool = True):
        _read_workbook_state(scan=scan, fetch_subs_if_empty=fetch_subs_if_empty)
        refresh_tables()

    def _refresh_ui_after_workbook_reload(*, pending_sync: bool = False) -> None:
        """Main-thread UI refresh after Collect / Push reloads state from disk."""
        refresh_tables()
        if pending_sync:
            _update_pending_sync()
        try:
            pending_tab.update()
        except (AssertionError, RuntimeError, Exception):
            pass

    def do_stop():
        if not state.busy:
            info("Nothing running to stop.")
            return
        job_cancel.set()
        info("Stop requested — will finish the current API call. If likes are only partly fetched, the workbook will not be overwritten.")

    def do_collect():
        if state.busy:
            info("Busy — wait for the current job to finish (Collect, Push, Match, or Sync check).")
            return
        state.busy = True
        job_cancel.clear()
        info("Collect started.")
        # Flush in-memory assigns so Collect's preserve map (disk) doesn't wipe them.
        try:
            _persist_workbook()
        except Exception:
            pass
        progress_start("collect", "Collect", 0)

        def _work():
            import time as _time

            err = None
            t0 = _time.time()
            try:
                def _p(done, total, msg):
                    progress_update("collect", done, total or None, msg)

                build_workbook(on_progress=_p, should_cancel=job_cancel.is_set)
                _read_workbook_state(scan=True)
            except Exception as e:
                err = e
                logger.exception("Collect failed")

            elapsed = _time.time() - t0
            cancelled = job_cancel.is_set()

            async def _finish():
                state.busy = False
                if err:
                    msg = str(err)
                    if "500" in msg or "502" in msg or "503" in msg or "504" in msg:
                        msg = (
                            f"{msg} — Sketchfab server error; wait a minute and retry Collect "
                            f"(your workbook was not overwritten)."
                        )
                    progress_done("collect", f"Collect failed after {format_duration(elapsed)}: {msg}")
                    if os.environ.get("SKETCHFAB_DEBUG"):
                        info(traceback.format_exc())
                elif cancelled:
                    progress_done(
                        "collect",
                        f"Collect stopped after {format_duration(elapsed)} — "
                        f"{len(state.liked_df):,} likes, {len(state.colls_df):,} collections written.",
                    )
                else:
                    progress_done(
                        "collect",
                        f"Collect done in {format_duration(elapsed)} — {len(state.liked_df):,} likes, "
                        f"{len(state.colls_df):,} collections, {len(state.subs_df):,} subscriptions.",
                    )
                if not err:
                    _refresh_ui_after_workbook_reload(pending_sync=True)
                    _run_background_probe(quiet=True)

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_quick_collect(*, background: bool = False):
        if state.busy:
            if not background:
                info("Busy — wait for the current job to finish.")
            return
        state.busy = True
        job_cancel.clear()
        if not background:
            info("Quick Collect started.")
        try:
            _persist_workbook()
        except Exception:
            pass
        progress_start("collect", "Quick Collect", 0)
        max_pages = int(app_settings.get("quick_collect_max_like_pages") or 20)

        def _work():
            import time as _time

            err = None
            t0 = _time.time()
            try:
                def _p(done, total, msg):
                    progress_update("collect", done, total or None, msg)

                build_workbook_quick(
                    on_progress=_p,
                    should_cancel=job_cancel.is_set,
                    max_like_pages=max_pages,
                )
                _read_workbook_state(scan=False)
            except Exception as e:
                err = e
                logger.exception("Quick Collect failed")

            elapsed = _time.time() - t0

            async def _finish():
                state.busy = False
                if err:
                    progress_done(
                        "collect",
                        f"Quick Collect failed after {format_duration(elapsed)}: {err}",
                    )
                else:
                    progress_done(
                        "collect",
                        f"Quick Collect done in {format_duration(elapsed)} — "
                        f"{len(state.liked_df):,} likes · {len(state.colls_df):,} collections.",
                    )
                    _refresh_ui_after_workbook_reload(pending_sync=True)
                    _run_background_probe(quiet=True)

            page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def _apply_probe_to_ui(probe, *, quiet: bool = False) -> bool:
        """Update drift banner; return True if material drift."""
        delta = probe.like_count_delta
        bits: list[str] = []
        if delta is not None:
            api_n = probe.account.like_count
            bits.append(f"API {api_n:,} likes vs wb {probe.wb_like_count:,}")
        if probe.collection_drifts:
            bits.append(f"{len(probe.collection_drifts)} col drift")
        if probe.new_like_uids:
            bits.append(f"+{len(probe.new_like_uids)} new")
        if probe.removed_like_uids:
            bits.append(f"-{len(probe.removed_like_uids)} unliked")
        sync_drift_ref.value = " · ".join(bits)
        refresh_counts()
        drifted = bool(
            (delta not in (None, 0))
            or probe.new_like_uids
            or probe.removed_like_uids
            or probe.collection_drifts
        )
        if drifted and app_settings.get("notify_on_sync_drift", True) and not quiet:
            info(f"Sync drift — {format_probe_summary(probe)}")
        elif not drifted:
            sync_drift_ref.value = "API in sync" if delta == 0 else sync_drift_ref.value
            refresh_counts()
        return drifted

    def _run_background_probe(*, quiet: bool = False) -> None:
        if state.busy or _shutting_down[0]:
            return
        if not os.environ.get("SKETCHFAB_TOKEN"):
            return

        def _work():
            try:
                probe = run_sync_probe(
                    state.liked_df,
                    state.colls_df,
                    state.subs_df,
                    max_like_pages=2,
                    check_collections=True,
                    light=True,
                )

                async def _finish():
                    if probe.message.startswith("Account probe failed"):
                        if not quiet:
                            info(f"Sync check skipped — {probe.message}")
                        return
                    drifted = _apply_probe_to_ui(probe, quiet=quiet)
                    if (
                        drifted
                        and app_settings.get("auto_quick_collect_on_drift")
                        and not state.busy
                    ):
                        do_quick_collect(background=True)

                page.run_task(_finish)
            except Exception:
                logger.exception("Background probe failed")
                if not quiet:
                    async def _probe_err():
                        info("Sync check failed — Sketchfab API may be down.")

                    try:
                        page.run_task(_probe_err)
                    except Exception:
                        pass

        threading.Thread(target=_work, daemon=True).start()

    def _schedule_background_sync() -> None:
        if _bg_sync_timer[0]:
            try:
                _bg_sync_timer[0].cancel()
            except Exception:
                pass
            _bg_sync_timer[0] = None
        if _shutting_down[0] or not app_settings.get("background_sync_enabled", True):
            return
        sec = max(15, int(app_settings.get("background_sync_interval_sec") or 60))

        def _tick():
            _run_background_probe(quiet=True)
            _schedule_background_sync()

        t = threading.Timer(sec, _tick)
        t.daemon = True
        _bg_sync_timer[0] = t
        t.start()

    def _on_settings_saved(new_settings: dict) -> None:
        nonlocal app_settings
        app_settings = dict(new_settings)
        dry_run_ref.set(bool(app_settings.get("dry_run_default", False)))
        info("Settings saved.")
        _schedule_background_sync()

    settings_tab.set_on_save(_on_settings_saved)

    def do_match():
        if state.busy:
            return
        state.busy = True
        page.splash = ft.ProgressBar()
        page.update()
        try:
            info("Computing Suggested/Fuzzy…")
            likes, colls, subs = read_workbook(scan_downloads=False)
            likes2 = run_auto_assign(likes, overwrite=False)
            if not _persist_workbook(liked=likes2, colls=colls, subs=subs):
                return
            state.liked_df = _noneify(enrich_download_status(likes2))
            refresh_tables()
            info("Match complete.")
        except Exception as e:
            info(f"Match failed: {e}")
            info(traceback.format_exc())
        finally:
            state.busy = False
            page.splash = None
            page.update()

    def do_auto_assign():
        if state.busy:
            return
        state.busy = True
        page.splash = ft.ProgressBar()
        page.update()
        try:
            info(f"Auto-assign (overwrite={overwrite_ref.value})…")
            likes, colls, subs = read_workbook(scan_downloads=False)
            likes2 = run_auto_assign(likes, overwrite=overwrite_ref.value)
            if not _persist_workbook(liked=likes2, colls=colls, subs=subs):
                return
            state.liked_df = _noneify(enrich_download_status(likes2))
            refresh_tables()
            info("Auto-assign complete.")
        finally:
            state.busy = False
            page.splash = None
            page.update()

    auto_assign_tab.set_callbacks(
        on_assign=do_bulk_assign,
        on_run_pipeline=do_auto_assign,
    )

    def do_apply_manual():
        if state.liked_df.empty:
            return
        df = state.liked_df.copy()
        if "Manual" in df.columns and "Assigned Collection(s)" in df.columns:
            mask = df["Manual"].fillna("").astype(str).str.strip() != ""
            df["Assigned Collection(s)"] = _coerce_str_series(df["Assigned Collection(s)"])
            df["Manual"] = _coerce_str_series(df["Manual"])
            df.loc[mask, "Assigned Collection(s)"] = df.loc[mask, "Manual"]
            if not _persist_workbook(liked=df):
                return
            state.liked_df = df
            refresh_tables()
            info(f"Applied Manual → Assigned on {int(mask.sum())} rows.")
        else:
            info("Manual / Assigned columns not present.")

    def do_save():
        _persist_workbook(ok_msg="Saved workbook to {path}")

    def do_scan_downloads():
        if state.busy:
            return
        state.busy = True
        page.splash = ft.ProgressBar()
        page.update()
        try:
            dirs = ensure_download_dirs()
            if not dirs:
                info("Could not create download folder — check SKETCHFAB_DOWNLOAD_ROOT in .env (e.g. F:\\)")
            else:
                info(f"Scanning {len(dirs)} folder(s) for local downloads…")
                state.liked_df = _noneify(enrich_download_status(state.liked_df))
                if _persist_workbook():
                    refresh_tables()
                    n = int((state.liked_df["Downloaded"].astype(str).str.lower() == "yes").sum())
                    info(f"Scan complete — {n:,} models found on disk.")
        finally:
            state.busy = False
            page.splash = None
            page.update()

    def do_scan_originals():
        if state.busy:
            return
        state.busy = True
        page.splash = ft.ProgressBar()
        page.update()

        def _work():
            err = None
            yes = 0
            try:
                from original_source import ORIGINAL_COL, enrich_original_sources

                progress_start("originals", "Scan originals", 0)

                def _progress(done: int, total: int, uid: str):
                    progress_update("originals", done, total, f"{done:,}/{total:,}")

                state.liked_df = _noneify(
                    enrich_original_sources(
                        state.liked_df,
                        SketchfabClient(),
                        only_missing=True,
                        peek_names=True,
                        downloadable_only=True,
                        on_progress=_progress,
                    )
                )
                if ORIGINAL_COL in state.liked_df.columns:
                    yes = int(
                        (state.liked_df[ORIGINAL_COL].astype(str).str.strip().str.lower() == "yes").sum()
                    )
                if not _persist_workbook():
                    return
            except Exception as e:
                err = e
            finally:
                async def _finish():
                    state.busy = False
                    page.splash = None
                    if err:
                        progress_done("originals", f"Originals scan failed: {err}")
                    else:
                        refresh_tables()
                        progress_done(
                            "originals",
                            f"Originals scan done — {yes:,} with author source archive.",
                        )
                    page.update()

                page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_push(*, force_real: bool = False):
        if state.busy:
            info("Busy — wait for the current job to finish, or click Stop on the toolbar.")
            return
        state.busy = True
        job_cancel.clear()

        def _work():
            from push_assignments import push, apply_push_markers, apply_unlisted_markers, summarize_push_result

            err = None
            res = None
            ui_refresh = False
            try:
                dry = False if force_real else dry_run_ref.value
                if force_real and dry_run_ref.value:
                    info("Push now — Dry-run OFF for this run (toolbar Dry-run switch ignored).")
                label = "Push" if not dry else "Push (dry-run)"
                progress_start("push", label, 0)
                likes = state.liked_df.copy()
                colls = state.colls_df.copy()
                if likes.empty:
                    progress_done("push", "No liked models loaded — open workbook or run Collect first.")
                    return

                def _progress(done: int, total: int, msg: str):
                    progress_update("push", done, total, msg)

                res = push(
                    likes,
                    colls,
                    client=SketchfabClient.for_push(),
                    dry_run=dry,
                    on_progress=_progress,
                    should_cancel=job_cancel.is_set,
                )
                created_colls = res.get("created_collections") or []
                if created_colls and not dry:
                    from push_assignments import merge_created_collections

                    state.colls_df = merge_created_collections(state.colls_df, created_colls)
                    ui_refresh = True
                for line in summarize_push_result(res):
                    info(line)

                pairs = res.get("posted_pairs") or []
                uids = res.get("posted_model_uids") or []
                unlisted = res.get("unlisted_pairs") or []
                if not dry and (pairs or uids or unlisted):
                    try:
                        if pairs or uids:
                            state.liked_df = apply_push_markers(
                                state.liked_df,
                                posted_model_uids=uids,
                                posted_pairs=pairs or None,
                            )
                        if unlisted:
                            state.liked_df = apply_unlisted_markers(
                                state.liked_df,
                                unlisted_pairs=unlisted,
                            )
                        if _persist_workbook():
                            ui_refresh = True
                            parts = []
                            if pairs or uids:
                                parts.append(
                                    f"Verified {len(pairs) or len(uids):,} assignment(s) on Sketchfab"
                                )
                            if unlisted:
                                n_u = len({u for u, _ in unlisted if u})
                                parts.append(f"shelved {n_u:,} as Unlisted (private)")
                            info(
                                " — ".join(parts)
                                + ". Cleared from Pending where applicable."
                            )
                    except Exception as mark_exc:
                        logger.exception("Push markers failed after successful API posts")
                        info(
                            f"Sketchfab sync succeeded, but workbook Push Sent markers failed: {mark_exc}. "
                            "Re-Push later to refresh markers (already-posted models are skipped)."
                        )
                elapsed = res.get("elapsed_sec", 0) if res else 0
                if res and res.get("cancelled"):
                    done_msg = f"Push stopped after {format_duration(elapsed)} — posted work saved."
                elif res and not dry:
                    done_msg = f"Push finished in {format_duration(elapsed)}."
                elif res:
                    done_msg = f"Dry-run finished in {format_duration(elapsed)}."
                else:
                    done_msg = "Push finished." if not dry else "Dry-run finished."
                progress_done("push", done_msg)
            except Exception as exc:
                err = exc
                logger.exception("Push failed")
                try:
                    progress_done("push", f"Push failed: {exc}")
                except Exception:
                    pass
            finally:
                refresh = ui_refresh

                async def _finish():
                    state.busy = False
                    if refresh:
                        refresh_tables()
                        try:
                            pending_tab.mark_dirty(state.liked_df)
                        except Exception:
                            pass
                    if err:
                        info(f"Push failed: {err}")
                        if os.environ.get("SKETCHFAB_DEBUG"):
                            info(traceback.format_exc())
                    safe_page_update(page)

                page.run_task(_finish)

        threading.Thread(target=_work, daemon=True).start()

    def do_merge():
        info("Scanning for similar collection names…")
        try:
            _, colls, _subs = read_workbook(scan_downloads=False)
            pairs = find_similar_collections(colls)
            if not pairs:
                info("No similar collections found (known OK pairs in terms/similar_ok.yaml are skipped).")
            else:
                for i, j, score in pairs[:10]:
                    a = colls.iloc[i]["Collection Name"]
                    b = colls.iloc[j]["Collection Name"]
                    info(f"Similar ({score}%): '{a}' ↔ '{b}'")
                info("Review these in Report — add intentional pairs to terms/similar_ok.yaml to silence.")
            report_tab.set_data(
                state.liked_df, state.colls_df, pairs,
                hide_nsfw=state.hide_nsfw, hide_female=state.hide_female, hide_male=state.hide_male,
            )
        except Exception as e:
            info(f"Merge scan failed: {e}")

    token_ok = "ok" if os.environ.get("SKETCHFAB_TOKEN") else "missing"
    status_trailing = [
        ft.Text(f"|  {XL_PATH}", size=11, color=ft.Colors.GREY_500),
        ft.Text(f"Token: {token_ok}", size=11, color=ft.Colors.GREEN_400 if token_ok == "ok" else ft.Colors.RED_400),
        ft.Text("|", size=11, color=ft.Colors.GREY_600),
        counts,
    ]

    toolbar = build_toolbar(
        on_collect=do_collect,
        on_match=do_match,
        on_auto_assign=do_auto_assign,
        on_apply_manual=do_apply_manual,
        on_save=do_save,
        on_push=do_push,
        on_merge=do_merge,
        on_scan_downloads=do_scan_downloads,
        on_scan_originals=do_scan_originals,
        overwrite_ref=overwrite_ref,
        dry_run_ref=dry_run_ref,
        auto_push_ref=auto_push_ref,
        hide_nsfw_ref=hide_nsfw_ref,
        hide_female_ref=hide_female_ref,
        hide_male_ref=hide_male_ref,
        on_quick_assign=do_quick_assign,
        on_content_filter_change=do_content_filter_change,
        on_stop=do_stop,
        on_quick_collect=do_quick_collect,
        status_trailing=status_trailing,
    )

    def _on_tab_change(e):
        idx = getattr(e.control, "selected_index", None)
        # Leaving a tab clears selection (detail pane too).
        try:
            liked_tab.clear_selection(emit=False)
        except Exception:
            pass
        try:
            browse_tab.clear_selection(emit=False)
        except Exception:
            pass
        detail_pane.show_selection([])
        if idx == 0:
            try:
                liked_tab.on_tab_shown()
            except Exception:
                pass
            try:
                if liked_tab._lod_debouncer:
                    liked_tab._lod_debouncer.kick(
                        page,
                        scroll=getattr(liked_tab, "_scroll_offset", 0.0) or 0.0,
                        viewport=getattr(liked_tab, "_viewport_h", 800.0) or 800.0,
                        remount_delay=0.08,
                    )
            except Exception:
                pass
            log.flush()
        elif idx == 1:
            pending_tab.set_df(state.liked_df)
            _update_pending_sync()
            log.flush()
        elif idx == 2:
            try:
                auto_assign_tab.set_df(state.liked_df)
            except Exception:
                pass
            log.flush()
        elif idx == 3:
            browse_tab.on_tab_shown()
            log.flush()
        elif idx == 4:
            try:
                inspector_tab.on_tab_shown()
            except Exception:
                pass
            log.flush()
        elif idx == 5:
            account_tab.on_tab_shown()
            log.flush()
        elif idx == 7:
            browse_colls_tab.on_tab_shown()
            log.flush()
        else:
            log.flush()

    def _scroll_active_tab_to_top(_e=None) -> None:
        idx = int(getattr(tabs, "selected_index", 0) or 0)
        targets = [
            liked_tab,
            pending_tab,
            auto_assign_tab,
            browse_tab,
            inspector_tab,
            account_tab,
            colls_tab,
            browse_colls_tab,
            subs_tab,
            None,  # Report
            None,  # Changelog
        ]
        tab = targets[idx] if 0 <= idx < len(targets) else None
        if tab is not None and hasattr(tab, "scroll_to_top"):
            try:
                tab.scroll_to_top()
            except Exception:
                pass

    to_top_btn = ft.TextButton(
        "TO TOP",
        icon=ft.Icons.VERTICAL_ALIGN_TOP,
        tooltip="Scroll the current tab to the top",
        style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=8, vertical=0)),
        on_click=_scroll_active_tab_to_top,
    )

    tabs = ft.Tabs(
        expand=True,
        on_change=_on_tab_change,
        tabs=[
            ft.Tab(text="Liked Models", content=liked_tab),
            ft.Tab(text="Pending Push", content=pending_tab),
            ft.Tab(text="Auto-Assign", content=auto_assign_tab),
            ft.Tab(text="Browse", content=browse_tab),
            ft.Tab(text="3D Inspector", content=inspector_tab),
            ft.Tab(text="Account", content=account_tab),
            ft.Tab(text="My Collections", content=colls_tab),
            ft.Tab(text="Browse Collections", content=browse_colls_tab),
            ft.Tab(text="Subscribed", content=subs_tab),
            ft.Tab(text="Report / Terms", content=report_tab),
            ft.Tab(text="Changelog", content=changelog_tab),
            ft.Tab(text="Settings", content=settings_tab),
        ],
    )

    tabs_ctrl[0] = tabs

    tabs_with_to_top = ft.Stack(
        expand=True,
        controls=[
            tabs,
            ft.Container(
                top=2,
                right=4,
                content=to_top_btn,
            ),
        ],
    )

    def _exit_app(_e=None):
        _shutting_down[0] = True
        threading.Thread(target=stop_tray_icon, daemon=True, name="stop-tray").start()
        force_exit(page.title, fast=True)

    settings_tab.set_on_exit(_exit_app)

    def _restore_from_tray_ui():
        restore_from_tray(page, title_hint=page.title)
        if app_settings.get("hide_console_on_tray", True):
            set_console_visible(True)

    def _hide_tray(_e=None):
        if not hide_to_tray(page, title_hint=page.title):
            info("Hide to tray failed.")
            return
        if app_settings.get("hide_console_on_tray", True):
            set_console_visible(False)

        async def _msg():
            if _tray_ready[0]:
                info("Hidden to tray — tray Show restores app + console.")
            else:
                info("Window hidden — tray icon unavailable; restore from taskbar or restart.")

        page.run_task(_msg)

    _resize_tabs = [
        liked_tab,
        pending_tab,
        auto_assign_tab,
        browse_tab,
        inspector_tab,
        account_tab,
        colls_tab,
        browse_colls_tab,
        subs_tab,
    ]
    _resize_token = [0]
    _last_relayout_w = [0.0]
    _window_dragging = [False]
    _move_viewport_tok = [0]

    def _sync_active_tab_viewport() -> None:
        idx = int(getattr(tabs, "selected_index", 0) or 0)
        if 0 <= idx < len(_resize_tabs):
            tab = _resize_tabs[idx]
            fn = getattr(tab, "on_viewport_sync", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    def _schedule_viewport_sync() -> None:
        _move_viewport_tok[0] += 1
        tok = _move_viewport_tok[0]

        async def _tick():
            await asyncio.sleep(0)
            if tok != _move_viewport_tok[0] or _shutting_down[0]:
                return
            _sync_active_tab_viewport()

        try:
            page.run_task(_tick)
        except Exception:
            pass

    def _schedule_active_tab_relayout(*, delay_s: float = 0.5) -> None:
        if relayout_suppressed():
            return
        _resize_token[0] += 1
        tok = _resize_token[0]

        async def _later():
            await asyncio.sleep(delay_s)
            if tok != _resize_token[0] or _shutting_down[0] or relayout_suppressed():
                return
            try:
                w = float(getattr(page, "width", None) or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0 and abs(w - _last_relayout_w[0]) < 20:
                return
            _last_relayout_w[0] = w
            idx = int(getattr(tabs, "selected_index", 0) or 0)
            if 0 <= idx < len(_resize_tabs):
                tab = _resize_tabs[idx]
                if tab is not None and hasattr(tab, "on_page_resize"):
                    try:
                        tab.on_page_resize(w)
                    except Exception:
                        pass

        try:
            page.run_task(_later)
        except Exception:
            pass

    title_bar = None
    if caption_mode == "frameless":
        title_bar = build_frameless_title_bar(
            page,
            title=page.title,
            on_hide_tray=_hide_tray,
            on_exit=_exit_app,
            schedule_relayout=lambda d: _schedule_active_tab_relayout(delay_s=d),
        )

    app_body = ft.Container(
        expand=True,
        padding=0,
        content=ft.Column(
            expand=True,
            spacing=6,
            controls=[
                toolbar,
                ft.Row(
                    [
                        ft.Container(expand=True, content=tabs_with_to_top),
                        ft.Container(
                            width=300,
                            expand=False,
                            border=ft.border.only(left=ft.BorderSide(1, "#334155")),
                            padding=ft.padding.only(left=10),
                            content=ft.Column(
                                [
                                    ft.Container(expand=2, content=log),
                                    ft.Divider(height=1, color="#334155"),
                                    ft.Container(expand=3, content=detail_pane),
                                    ft.Divider(height=1, color="#334155"),
                                    ft.Container(expand=2, content=dl_queue),
                                ],
                                expand=True,
                                spacing=6,
                            ),
                        ),
                    ],
                    expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ],
        ),
    )

    shell_controls: list[ft.Control]
    if title_bar is not None:
        shell_controls = [title_bar, app_body]
    else:
        shell_controls = [app_body]

    page.add(
        ft.Column(
            expand=True,
            spacing=0,
            controls=shell_controls,
        )
    )
    if caption_mode == "frameless":
        info("Title bar: dual × — gray hides to tray, red quits (— □ on the left).")
    else:
        info("Title bar: Windows native (enable Dual close in Settings + restart for gray/red ×).")
    info(f"⏱ UI shell built: {(time.perf_counter() - t_app_start) * 1000:.0f} ms")

    def _on_window_event(e: ft.WindowEvent) -> None:
        t = e.type
        if t == ft.WindowEventType.CLOSE:
            if _shutting_down[0]:
                return
            if app_settings.get("close_to_tray", False):
                try:
                    page.window.prevent_close = True
                except Exception:
                    pass
                _hide_tray()
            else:
                _exit_app()
            return
        # Position-only move: refresh visible-card LOD every frame (no grid relayout).
        if t == ft.WindowEventType.MOVE:
            _window_dragging[0] = True
            try:
                w = float(getattr(page, "width", None) or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0 and abs(w - _last_relayout_w[0]) < 4:
                _schedule_viewport_sync()
            elif w > 0:
                _last_relayout_w[0] = w
            return
        if t == ft.WindowEventType.MOVED:
            _window_dragging[0] = False
            _sync_active_tab_viewport()
            return
        if t == ft.WindowEventType.RESIZED:
            try:
                w = float(getattr(page, "width", None) or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                if abs(w - _last_relayout_w[0]) < 4:
                    _schedule_viewport_sync()
                    return
                _last_relayout_w[0] = w
            if _window_dragging[0] or relayout_suppressed():
                return
            _schedule_active_tab_relayout(delay_s=0.45)

    try:
        page.window.prevent_close = bool(app_settings.get("close_to_tray", False))
        page.window.on_event = _on_window_event
    except Exception:
        pass

    if app_settings.get("minimize_to_tray", True):
        tray_ok = start_tray_icon(
            title="Sketchfab Collections",
            ui_queue=_tray_ui_queue,
        )
        _tray_ready[0] = tray_ok

        async def _tray_ui_loop():
            import asyncio

            while not _shutting_down[0]:
                try:
                    cmd = _tray_ui_queue.get_nowait()
                except Exception:
                    await asyncio.sleep(0.12)
                    continue
                if cmd == "show":
                    _restore_from_tray_ui()
                elif cmd == "exit":
                    _exit_app()

        page.run_task(_tray_ui_loop)

        if tray_ok:
            async def _tray_ok_msg():
                if caption_mode != "native":
                    info("Tray active — gray × hides; red × quits; Show in tray restores.")
                else:
                    info("Tray active — × quits app; — hides to tray; tray Show restores.")

            page.run_task(_tray_ok_msg)
        elif caption_mode != "native":
            async def _tray_warn():
                info("Tray icon unavailable — pip install pystray Pillow")

            page.run_task(_tray_warn)

    import atexit

    def _on_app_exit() -> None:
        job_cancel.set()
        try:
            end_session(version=APP_VERSION)
        except Exception:
            pass

    atexit.register(_on_app_exit)

    async def _startup_load():
        """Paint likes ASAP from xlsx; defer heavy disk/API work."""
        t0 = time.perf_counter()
        liked_tab.show_loading("Loading likes…")

        try:
            loop = asyncio.get_running_loop()
            likes, colls, subs = await loop.run_in_executor(
                None, lambda: read_workbook(scan_downloads=False)
            )
            info(f"⏱ Workbook read: {(time.perf_counter() - t0) * 1000:.0f} ms")
            state.liked_df = _noneify(normalize_liked_columns(likes))
            state.colls_df = _noneify(colls)
            state.subs_df = _noneify(subs)

            state.hide_nsfw = hide_nsfw_ref.value
            state.hide_female = hide_female_ref.value
            state.hide_male = hide_male_ref.value
            liked_tab.set_content_filters(state.hide_nsfw, state.hide_female, state.hide_male)
            try:
                liked_tab.set_df(
                    state.liked_df,
                    state.liked_page,
                    state.liked_page_size,
                    state.colls_df,
                    refresh_dropdowns=False,
                    seed_pending=False,
                )
            except (AssertionError, RuntimeError, Exception):
                logger.exception("Liked first paint failed")
            refresh_counts()
            apply_pagers()
            try:
                _last_relayout_w[0] = float(getattr(page, "width", None) or 0)
            except (TypeError, ValueError):
                pass
            info(
                f"Ready — {len(state.liked_df):,} likes · {len(state.colls_df):,} collections"
                f" · {len(state.subs_df):,} subscribed · v{APP_VERSION}"
            )
            info(f"⏱ Startup to likes visible: {(time.perf_counter() - t_app_start) * 1000:.0f} ms")
            dl_dirs = ensure_download_dirs()
            if not dl_dirs:
                info("Set SKETCHFAB_DOWNLOAD_ROOT in .env (e.g. F:\\) for downloads / Scan DL.")
            page.update()

            async def _deferred_liked_chrome():
                await asyncio.sleep(0.02)
                if _shutting_down[0]:
                    return
                try:
                    liked_tab._refresh_dropdowns()
                    liked_tab._seed_pending_from_df()
                    page.update()
                except Exception:
                    logger.exception("Liked deferred chrome failed")

            page.run_task(_deferred_liked_chrome)

        except FileNotFoundError:
            info("No workbook yet — click Collect.")
            liked_tab.show_loading("No liked models yet — click Collect.")
            page.update()
        except Exception as e:
            logger.exception("Startup load failed")
            info(f"Startup load failed: {e}")
            liked_tab.show_loading(f"Load failed: {e}")
            page.update()

    async def _deferred_disk_scan():
        await asyncio.sleep(8)
        if _shutting_down[0]:
            return
        try:
            loop = asyncio.get_running_loop()
            enriched = await loop.run_in_executor(
                None, lambda: _noneify(enrich_download_status(state.liked_df))
            )
            state.liked_df = enriched
            liked_tab.bind_source(state.liked_df)
            n = liked_tab.paint_download_markers_from_df()
            refresh_counts()
            if n:
                info(f"On-disk markers updated — {n:,}.")
            info(f"⏱ Startup complete: {(time.perf_counter() - t_app_start) * 1000:.0f} ms")
            page.update()
        except Exception:
            logger.exception("Deferred download scan failed")

    async def _deferred_startup_extras():
        await asyncio.sleep(1.0)
        if _shutting_down[0]:
            return
        try:
            refresh_background_tabs()
        except Exception:
            logger.exception("Deferred tab refresh failed")
        _run_background_probe(quiet=True)

    page.run_task(_startup_load)
    page.run_task(_deferred_disk_scan)
    page.run_task(_deferred_startup_extras)
    _load_username()
    _load_categories()
    _schedule_background_sync()


if __name__ == "__main__":
    from session_log import log_file

    try:
        try:
            kill_stuck_flet_windows("Sketchfab")
        except Exception:
            pass
        ft.app(target=main)
    except Exception:
        err = traceback.format_exc()
        log_path = log_file(ROOT)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\nSTARTUP FAILED:\n{err}\n")
        except OSError:
            pass
        legacy = os.path.join(ROOT, "startup_error.log")
        try:
            with open(legacy, "w", encoding="utf-8") as f:
                f.write(err)
        except OSError:
            pass
        print(err)
        print(f"\nError written to: {log_path}")
        input("Press Enter to close...")
    else:
        try:
            end_session(version=APP_VERSION)
        except Exception:
            pass
        os._exit(0)
