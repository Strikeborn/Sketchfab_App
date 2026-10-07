"""Auto-Assign tab — bubble board + review panel before assigning."""
from __future__ import annotations

import flet as ft
import pandas as pd

from auto_assign import TERMS_PATH
from matching import Terms, collect_signals
from state import name_col, uid_col
from ui.scroll_drag import wrap_middle_drag_scroll

_PANEL = "#0f172a"
_BUBBLE_BG = "#1e3a5f"
_BUBBLE_BORDER = "#38bdf8"
_BUBBLE_SEL = "#67e8f9"
_MUTED = "#94a3b8"
_ROW_ALT = "#1a2332"


def _csv_parts(cell) -> list[str]:
    s = str(cell or "").strip()
    if not s or s.lower() in {"nan", "none", "<na>"}:
        return []
    return [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]


def _is_unlisted(row) -> bool:
    already = {p.casefold() for p in _csv_parts(row.get("Already In Collection(s)"))}
    return "unlisted" in already


def _is_unassigned(row) -> bool:
    if _is_unlisted(row):
        return False
    assigned = _csv_parts(row.get("Assigned Collection(s)"))
    already = _csv_parts(row.get("Already In Collection(s)"))
    if assigned:
        return False
    real = [a for a in already if a.casefold() != "unlisted"]
    return not real


def pool_df(liked_df: pd.DataFrame, *, mode: str = "unassigned") -> pd.DataFrame:
    """mode: unassigned | all (excludes Unlisted either way)."""
    if liked_df is None or liked_df.empty:
        return pd.DataFrame()
    rows = []
    for _, row in liked_df.iterrows():
        if _is_unlisted(row):
            continue
        if mode == "unassigned" and not _is_unassigned(row):
            continue
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=liked_df.columns)
    return pd.DataFrame(rows)


def count_unassigned(liked_df: pd.DataFrame) -> int:
    if liked_df is None or liked_df.empty:
        return 0
    return sum(1 for _, row in liked_df.iterrows() if _is_unassigned(row))


def fit_label(score: int, reason: str) -> str:
    """Human label for how right the collection looks for a model."""
    sc = int(score or 0)
    why = (reason or "").strip()
    if sc >= 95:
        tier = "Excellent"
    elif sc >= 90:
        tier = "Strong"
    elif sc >= 85:
        tier = "Likely"
    elif sc >= 75:
        tier = "Weak"
    else:
        tier = "Low"
    if why == "tag/rule":
        how = "tag / YAML rule"
    elif why == "fuzzy":
        how = "fuzzy title/name"
    else:
        how = why or "signal"
    return f"{tier} ({sc}) · {how}"


def fit_color(score: int) -> str:
    sc = int(score or 0)
    if sc >= 95:
        return "#4ade80"
    if sc >= 90:
        return "#67e8f9"
    if sc >= 85:
        return "#fbbf24"
    return "#f87171"


def build_bubbles(
    liked_df: pd.DataFrame,
    *,
    mode: str = "unassigned",
    terms: Terms | None = None,
    limit: int = 48,
) -> list[dict]:
    """
    Aggregate collection candidates from tags / rules / fuzzy.
    Each bubble: {name, count, uids, items, best_score, avg_score, reasons}
    items: [{uid, name, tags, score, reason, fit}]
    """
    terms = terms or Terms.from_yaml(TERMS_PATH)
    df = pool_df(liked_df, mode=mode)
    if df.empty:
        return []
    uc = uid_col(df)
    nc = name_col(df)
    agg: dict[str, dict] = {}

    for _, row in df.iterrows():
        uid = str(row.get(uc) or "").strip()
        if not uid:
            continue
        name = str(row.get(nc) or row.get("Name") or "")
        desc = str(row.get("Description") or "")
        tags = [t.strip() for t in str(row.get("Tags") or "").split(",") if t.strip()]
        sug = _csv_parts(row.get("Suggested Collection(s)"))
        fuzzy_raw = _csv_parts(row.get("Fuzzy Match Collection(s)"))
        fuzzy_map: dict[str, int] = {}
        for part in fuzzy_raw:
            if ":" in part:
                k, _, sc = part.partition(":")
                try:
                    fuzzy_map[k.strip()] = int(float(sc.strip()))
                except ValueError:
                    fuzzy_map[k.strip()] = 90
            elif part.strip():
                fuzzy_map[part.strip()] = 90

        if not sug and not fuzzy_map:
            signals = collect_signals(name, desc, tags, terms)
            sug = sorted(signals.tag_hits | signals.rule_hits)
            fuzzy_map = dict(signals.fuzzy_hits)

        candidates: dict[str, tuple[int, str]] = {}
        for c in sug:
            candidates[c] = (max(candidates.get(c, (0, ""))[0], 92), "tag/rule")
        for c, sc in fuzzy_map.items():
            if sc >= 85:
                prev = candidates.get(c, (0, ""))
                if sc >= prev[0]:
                    candidates[c] = (sc, "fuzzy")

        for coll, (score, reason) in candidates.items():
            bucket = agg.get(coll)
            if bucket is None:
                bucket = {"name": coll, "items": [], "uids_seen": set()}
                agg[coll] = bucket
            if uid in bucket["uids_seen"]:
                continue
            bucket["uids_seen"].add(uid)
            bucket["items"].append(
                {
                    "uid": uid,
                    "name": name or uid,
                    "tags": tags,
                    "score": int(score),
                    "reason": reason,
                    "fit": fit_label(score, reason),
                }
            )

    out = []
    for coll, b in agg.items():
        items = b["items"]
        scores = [int(it["score"]) for it in items] or [0]
        # Strongest first in review list
        items.sort(key=lambda it: (-int(it["score"]), str(it["name"]).casefold()))
        out.append(
            {
                "name": coll,
                "count": len(items),
                "uids": [it["uid"] for it in items],
                "items": items,
                "best_score": max(scores),
                "avg_score": sum(scores) / len(scores),
                "reasons": sorted({str(it.get("reason") or "") for it in items if it.get("reason")}),
            }
        )
    out.sort(key=lambda x: (-x["count"], -x["best_score"], x["name"].casefold()))
    return out[:limit]


class AutoAssignTab(ft.Column):
    def __init__(self, page: ft.Page | None = None):
        super().__init__(expand=True, spacing=8)
        self._page = page
        self._liked_df = pd.DataFrame()
        self._mode = "unassigned"  # unassigned | all
        self._on_assign = None  # (uids: list[str], collection: str) -> None
        self._on_run_pipeline = None
        self._bubbles: list[dict] = []
        self._selected: dict | None = None
        self._include: dict[str, bool] = {}  # uid -> include

        self._status = ft.Text("", size=13, color=_MUTED)
        self._mode_seg = ft.SegmentedButton(
            selected={"unassigned"},
            segments=[
                ft.Segment(value="unassigned", label=ft.Text("Unassigned"), icon=ft.Icon(ft.Icons.HELP_OUTLINE)),
                ft.Segment(value="all", label=ft.Text("All (ex Unlisted)"), icon=ft.Icon(ft.Icons.APPS)),
            ],
            on_change=self._on_mode,
        )
        self._refresh_btn = ft.OutlinedButton(
            "Refresh bubbles",
            icon=ft.Icons.REFRESH,
            on_click=lambda e: self.refresh(),
        )
        self._run_btn = ft.FilledButton(
            "Run Auto-Assign (YAML)",
            icon=ft.Icons.AUTO_FIX_HIGH,
            tooltip="Same as toolbar Auto-Assign — writes Suggested/Assigned via policy",
            on_click=lambda e: self._on_run_pipeline() if self._on_run_pipeline else None,
        )
        self._board = ft.Row(wrap=True, spacing=10, run_spacing=10)
        self._board_scroll = ft.Column(
            [
                ft.Text(
                    "Bubbles = candidate collections (not models already in that collection). "
                    "Click a bubble to review names/tags/fit, exclude junk, then Assign selected.",
                    size=11,
                    color=_MUTED,
                ),
                self._board,
            ],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

        self._review_title = ft.Text("Select a bubble to review", size=14, weight=ft.FontWeight.BOLD)
        self._review_meta = ft.Text("", size=11, color=_MUTED)
        self._review_list = ft.ListView(expand=True, spacing=0, padding=0)
        self._review_list_wrap = wrap_middle_drag_scroll(self._review_list, expand=True)
        self._assign_btn = ft.FilledButton(
            "Assign selected → …",
            icon=ft.Icons.DRIVE_FILE_MOVE,
            disabled=True,
            on_click=lambda e: self._assign_selected(),
        )
        self._sel_all_btn = ft.TextButton("Select all", on_click=lambda e: self._set_all_include(True))
        self._sel_none_btn = ft.TextButton("Select none", on_click=lambda e: self._set_all_include(False))
        self._sel_strong_btn = ft.TextButton(
            "Strong+ only",
            tooltip="Keep Excellent/Strong (≥90); uncheck Likely/Weak",
            on_click=lambda e: self._set_strong_only(),
        )

        self._review_panel = ft.Container(
            width=420,
            expand=True,
            bgcolor=_PANEL,
            border_radius=10,
            padding=12,
            border=ft.border.all(1, "#334155"),
            content=ft.Column(
                [
                    self._review_title,
                    self._review_meta,
                    ft.Row(
                        [self._sel_all_btn, self._sel_none_btn, self._sel_strong_btn, ft.Container(expand=True), self._assign_btn],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._review_list_wrap,
                ],
                spacing=8,
                expand=True,
            ),
        )

        self._board_wrap = ft.Container(
            expand=2,
            bgcolor=_PANEL,
            border_radius=10,
            padding=16,
            content=self._board_scroll,
        )

        self.controls = [
            ft.Row(
                [
                    ft.Text("Auto-Assign", size=16, weight=ft.FontWeight.BOLD),
                    self._status,
                    ft.Container(expand=True),
                    self._mode_seg,
                    self._refresh_btn,
                    self._run_btn,
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.Row(
                [self._board_wrap, self._review_panel],
                spacing=10,
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        ]

    def set_callbacks(self, *, on_assign=None, on_run_pipeline=None) -> None:
        self._on_assign = on_assign
        self._on_run_pipeline = on_run_pipeline

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        pass

    def set_df(self, liked_df: pd.DataFrame) -> None:
        self._liked_df = liked_df if isinstance(liked_df, pd.DataFrame) else pd.DataFrame()
        self.refresh()

    def _on_mode(self, e) -> None:
        sel = e.control.selected or {"unassigned"}
        self._mode = "all" if "all" in sel else "unassigned"
        self.refresh()

    def refresh(self) -> None:
        un_n = count_unassigned(self._liked_df)
        pool_n = len(pool_df(self._liked_df, mode=self._mode))
        self._status.value = (
            f"{un_n:,} unassigned  ·  pool {pool_n:,} "
            f"({'unassigned only' if self._mode == 'unassigned' else 'all except Unlisted'})"
        )
        sel_name = str((self._selected or {}).get("name") or "")
        try:
            self._bubbles = build_bubbles(self._liked_df, mode=self._mode)
        except Exception as ex:
            self._bubbles = []
            self._board.controls = [
                ft.Text(f"Bubble build failed: {ex}", color=ft.Colors.RED_300, size=12)
            ]
            self._clear_review()
            try:
                self.update()
            except Exception:
                pass
            return
        self._board.controls = [self._bubble_ctrl(b) for b in self._bubbles] or [
            ft.Text(
                "No strong candidates — run Match first, or widen to All (ex Unlisted).",
                size=12,
                color=_MUTED,
            )
        ]
        # Re-open review if same collection still exists
        if sel_name:
            match = next((b for b in self._bubbles if b["name"] == sel_name), None)
            if match:
                self._open_review(match, keep_excludes=True)
            else:
                self._clear_review()
        else:
            self._paint_review_empty()
        try:
            self.update()
        except Exception:
            pass

    def _bubble_ctrl(self, b: dict) -> ft.Container:
        n = int(b.get("count") or 0)
        pad = 10 + min(18, n // 3)
        best = int(b.get("best_score") or 0)
        avg = float(b.get("avg_score") or 0)
        name = str(b.get("name") or "")
        selected = bool(self._selected and self._selected.get("name") == name)
        tip = (
            f"{n:,} candidates for '{name}'\n"
            f"Best fit {best} · avg {avg:.0f}\n"
            f"Click to review include/exclude before assigning"
        )
        return ft.Container(
            bgcolor=_BUBBLE_BG,
            border=ft.border.all(2 if selected else 1, _BUBBLE_SEL if selected else _BUBBLE_BORDER),
            border_radius=999,
            padding=ft.padding.symmetric(horizontal=pad + 4, vertical=pad),
            tooltip=tip,
            ink=True,
            on_click=lambda e, bubble=b: self._open_review(bubble),
            content=ft.Column(
                [
                    ft.Text(name, size=12, weight=ft.FontWeight.W_600, color="#e2e8f0", no_wrap=True),
                    ft.Text(f"{n:,} candidates", size=10, color="#e2e8f0"),
                    ft.Text(
                        f"best {best} · avg {avg:.0f}",
                        size=10,
                        color=fit_color(best),
                    ),
                    ft.Text("Review →", size=10, color="#67e8f9"),
                ],
                spacing=2,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
        )

    def _clear_review(self) -> None:
        self._selected = None
        self._include.clear()
        self._paint_review_empty()

    def _paint_review_empty(self) -> None:
        self._review_title.value = "Select a bubble to review"
        self._review_meta.value = "Candidates are Match/tag/fuzzy hits — not models already in the collection."
        self._review_list.controls = []
        self._assign_btn.text = "Assign selected → …"
        self._assign_btn.disabled = True

    def _open_review(self, bubble: dict, *, keep_excludes: bool = False) -> None:
        items = list(bubble.get("items") or [])
        prev_excl = {u for u, on in self._include.items() if not on} if keep_excludes else set()
        self._selected = bubble
        self._include = {
            str(it["uid"]): (str(it["uid"]) not in prev_excl)
            for it in items
            if it.get("uid")
        }
        name = str(bubble.get("name") or "")
        n = len(items)
        best = int(bubble.get("best_score") or 0)
        avg = float(bubble.get("avg_score") or 0)
        self._review_title.value = f"→ {name}"
        self._review_meta.value = (
            f"{n:,} candidates · best fit {best} · avg {avg:.0f} · "
            f"uncheck models that don’t belong, then Assign"
        )
        self._review_list.controls = [
            self._review_row(it, i) for i, it in enumerate(items)
        ]
        self._sync_assign_btn()
        # Re-paint bubbles for selection border
        self._board.controls = [self._bubble_ctrl(b) for b in self._bubbles]
        try:
            self.update()
        except Exception:
            pass

    def _review_row(self, it: dict, idx: int) -> ft.Container:
        uid = str(it.get("uid") or "")
        name = str(it.get("name") or uid)
        tags = it.get("tags") or []
        tag_s = ", ".join(tags[:8]) + ("…" if len(tags) > 8 else "")
        score = int(it.get("score") or 0)
        fit = str(it.get("fit") or fit_label(score, str(it.get("reason") or "")))
        cb = ft.Checkbox(
            value=bool(self._include.get(uid, True)),
            on_change=lambda e, u=uid: self._on_toggle(u, bool(e.control.value)),
        )
        return ft.Container(
            bgcolor=_ROW_ALT if idx % 2 else None,
            padding=ft.padding.symmetric(horizontal=6, vertical=4),
            content=ft.Row(
                [
                    cb,
                    ft.Column(
                        [
                            ft.Text(
                                name,
                                size=12,
                                weight=ft.FontWeight.W_500,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                tag_s or "(no tags)",
                                size=10,
                                color=_MUTED,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(fit, size=10, color=fit_color(score)),
                        ],
                        spacing=1,
                        expand=True,
                        tight=True,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )

    def _on_toggle(self, uid: str, included: bool) -> None:
        self._include[uid] = included
        self._sync_assign_btn()
        try:
            self._assign_btn.update()
            self._review_meta.update()
        except Exception:
            try:
                self.update()
            except Exception:
                pass

    def _included_uids(self) -> list[str]:
        if not self._selected:
            return []
        return [
            str(it["uid"])
            for it in (self._selected.get("items") or [])
            if self._include.get(str(it["uid"]), True)
        ]

    def _sync_assign_btn(self) -> None:
        name = str((self._selected or {}).get("name") or "")
        n = len(self._included_uids())
        total = len((self._selected or {}).get("items") or [])
        self._assign_btn.text = f"Assign {n:,} → {name}" if name else "Assign selected → …"
        self._assign_btn.disabled = not (name and n)
        if self._selected:
            self._review_meta.value = (
                f"{total:,} candidates · {n:,} included · "
                f"best fit {int(self._selected.get('best_score') or 0)} · "
                f"avg {float(self._selected.get('avg_score') or 0):.0f}"
            )

    def _set_all_include(self, value: bool) -> None:
        if not self._selected:
            return
        for uid in list(self._include.keys()):
            self._include[uid] = value
        self._review_list.controls = [
            self._review_row(it, i) for i, it in enumerate(self._selected.get("items") or [])
        ]
        self._sync_assign_btn()
        try:
            self.update()
        except Exception:
            pass

    def _set_strong_only(self) -> None:
        if not self._selected:
            return
        for it in self._selected.get("items") or []:
            uid = str(it.get("uid") or "")
            self._include[uid] = int(it.get("score") or 0) >= 90
        self._review_list.controls = [
            self._review_row(it, i) for i, it in enumerate(self._selected.get("items") or [])
        ]
        self._sync_assign_btn()
        try:
            self.update()
        except Exception:
            pass

    def _assign_selected(self) -> None:
        if not self._selected or not self._on_assign:
            return
        name = str(self._selected.get("name") or "")
        uids = self._included_uids()
        if name and uids:
            self._on_assign(uids, name)
