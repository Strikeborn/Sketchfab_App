# main.py (slim)
from __future__ import annotations
import traceback
import flet as ft
import pandas as pd
from state import AppState
from ui.toolbar import build_toolbar
from ui.tabs_liked import LikedTab
from ui.tabs_collections import CollectionsTab
from ui.tabs_report import ReportTab
from ui.log_view import LogView
from ui.pager import build_pager
from data_io import read_workbook, write_workbook
from collector import build_workbook  # assumes your collector exposes this
from matching import Terms
from auto_assign import run_auto_assign
from push_assignments import push
from merge_collections import interactive_merge
print("MAIN FILE:", __file__)
class Ref:
    def __init__(self, value=None): self.value=value
    def set(self, v): self.value=v

def main(page: ft.Page):
    page.title = "Sketchfab Collections — Desktop"
    page.padding = 10
    page.scroll = ft.ScrollMode.AUTO

    state = AppState()
    log = LogView()

    liked_tab = LikedTab()
    colls_tab = CollectionsTab()
    report_tab = ReportTab(on_find_similar=lambda: do_merge())
    tabs = ft.Tabs(expand=True, tabs=[
        ft.Tab(text="Liked Models", content=liked_tab),
        ft.Tab(text="Collections", content=colls_tab),
        ft.Tab(text="Report / Terms", content=report_tab),
    ])

    
    # counts (right side of second bar)
    counts = ft.Text("", selectable=False)
    second_bar = ft.Row([tabs, counts], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    overwrite_ref = Ref(False)
    dry_run_ref = Ref(True)

    def refresh_counts():
        counts.value = f"Liked: {len(state.liked_df)} • Collections: {len(state.colls_df)}"
        counts.update()

    def apply_pagers():
        # rebuild pagers each time counts/pages change
        liked_pager = build_pager(
            total_rows=len(state.liked_df),
            page=state.liked_page,
            page_size=state.page_size,
            on_change_page=lambda p: (setattr(state, "liked_page", p), liked_tab.set_df(state.liked_df, p, state.page_size)),
            on_change_size=lambda s: (setattr(state, "page_size", s), setattr(state, "liked_page", 0), liked_tab.set_df(state.liked_df, 0, s)),
        )
        colls_pager = build_pager(
            total_rows=len(state.colls_df),
            page=state.colls_page,
            page_size=state.page_size,
            on_change_page=lambda p: (setattr(state, "colls_page", p), colls_tab.set_df(state.colls_df, p, state.page_size)),
            on_change_size=lambda s: (setattr(state, "page_size", s), setattr(state, "colls_page", 0), colls_tab.set_df(state.colls_df, 0, s)),
        )
        liked_tab.pager.controls = [liked_pager]
        colls_tab.pager.controls = [colls_pager]
        liked_tab.update(); colls_tab.update()

    def refresh_tables():
        liked_tab.set_df(state.liked_df, state.liked_page, state.page_size)
        colls_tab.set_df(state.colls_df, state.colls_page, state.page_size)
        refresh_counts()
        apply_pagers()

    def info(msg): log.append(msg); page.update()

    def do_collect():
        if state.busy: 
            return
        state.busy = True; page.splash = ft.ProgressBar(); page.update()
        try:
            info("Collecting from Sketchfab…")
            build_workbook()                      # writes data/sketchfab_data.xlsx
            likes, colls = read_workbook()        # load back into DataFrames
            info(f"Loaded: liked={len(likes)} rows, collections={len(colls)} rows")
            state.liked_df, state.colls_df = likes, colls
            refresh_tables()
            info("Collect done.")
        finally:
            state.busy = False; page.splash = None; page.update()

    def do_match():
        if state.busy:
            return
        state.busy = True; page.splash = ft.ProgressBar(); page.update()
        try:
            info("Computing Suggested/Fuzzy…")
            likes, colls = read_workbook()
            # Non-destructive refresh: keep existing Assigned as-is
            likes2 = run_auto_assign(likes, overwrite=False)
            write_workbook(likes2, colls)
            state.liked_df, state.colls_df = likes2, colls
            refresh_tables()
            info("Match complete.")
        except Exception as e:
            info(f"❌ Match failed: {e}")
            info(traceback.format_exc())
        finally:
            state.busy = False; page.splash = None; page.update()

    def do_auto_assign():
        if state.busy: return
        state.busy = True; page.splash = ft.ProgressBar(); page.update()
        try:
            info(f"Auto-assign (overwrite={overwrite_ref.value})…")
            likes, colls = read_workbook()
            likes2 = run_auto_assign(likes, overwrite=overwrite_ref.value)
            write_workbook(likes2, colls)
            state.liked_df = likes2
            refresh_tables()
            info("Auto-assign complete.")
        finally:
            state.busy = False; page.splash = None; page.update()

    def do_apply_manual():
        # Copy Manual → Assigned for non-empty Manual cells
        if state.liked_df.empty: return
        df = state.liked_df.copy()
        if "Manual" in df.columns and "Assigned Collection(s)" in df.columns:
            mask = df["Manual"].fillna("").astype(str).str.strip() != ""
            df.loc[mask, "Assigned Collection(s)"] = df.loc[mask, "Manual"]
            write_workbook(df, state.colls_df)
            state.liked_df = df
            refresh_tables()
            info(f"Applied Manual → Assigned on {int(mask.sum())} rows.")
        else:
            info("Manual / Assigned columns not present.")

    def do_save():
        saved_path = write_workbook(state.liked_df, state.colls_df)
        info(f"Saved workbook to {saved_path}")


    def do_push():
        if state.busy: return
        state.busy = True; page.splash = ft.ProgressBar(); page.update()
        try:
            info(f"Pushing Assigned to Sketchfab (dry_run={dry_run_ref.value})…")
            likes, colls = read_workbook()
            res = push(likes, colls, dry_run=dry_run_ref.value)
            info(f"Push result: {res}")
        finally:
            state.busy = False; page.splash = None; page.update()

    def do_merge():
        info("Scanning for similar collections…")
        likes, colls = read_workbook()
        changes_made = interactive_merge(colls)  # your function already handles UI/logic
        if changes_made:
            write_workbook(likes, colls)
            state.colls_df = colls
            refresh_tables()
            info("Merge complete.")

    toolbar = build_toolbar(
        on_collect=do_collect,
        on_match=do_match,
        on_auto_assign=do_auto_assign,
        on_apply_manual=do_apply_manual,
        on_save=do_save,
        on_push=do_push,
        on_merge=do_merge,
        overwrite_ref=overwrite_ref,
        dry_run_ref=dry_run_ref,
    )
    
    page.add(toolbar, ft.Divider(), second_bar, ft.Divider(), log)

        # now it's safe to log + load workbook
    try:
        likes0, colls0 = read_workbook()
        state.liked_df, state.colls_df = likes0, colls0
        refresh_tables()
        info(f"Startup load: liked={len(likes0)} rows, collections={len(colls0)} rows")
    except Exception:
        info("No workbook yet — click Collect.")

if __name__ == "__main__":
    ft.app(target=main)
