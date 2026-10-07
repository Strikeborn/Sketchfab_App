"""Account tab — inspect a Sketchfab user's models, likes, collections, subscriptions."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import flet as ft

from browse_models import normalize_search_model
from collection_urls import collection_public_url
from content_filter import filter_rows, passes_content_filter
from ui.scroll_drag import wrap_middle_drag_scroll
from ui.viewer_place import screen_rect_for_card_image
from ui.liked_style import download_icon_style
from ui.download_queue import downloads_busy
from ui.thumb_lod import make_lod_thumb, ViewportLod, ScrollLodDebouncer

_PANEL = "#0f172a"
_MIN_COLS = 2
_MAX_COLS = 8
_DEFAULT_COLS = 4
_RAIL_TINT = ft.Colors.with_opacity(0.94, "#020617")
_META_COLOR = "#fef08a"
_RAIL_W = 36
_MIN_CARD_W = 152
_FALLBACK_GRID_AVAIL = 1100
_GRID_GAP = 8
_GRID_SIDE_CHROME = 340
_FIND_MIN_CARD_W = 280
_FIND_THUMB = 72
_FIND_DEFAULT_COLS = 2
_FIND_PREFETCH_WORKERS = 2
_FIND_CARD_ROW_H = 118

_USER_FIND_SORTS = [
    ("-followerCount", "Most followers"),
    ("-modelCount", "Most models"),
    ("_likeCount", "Most likes"),
    ("_collectionCount", "Most collections"),
    ("_subscriptionCount", "Most subscriptions"),
    ("_followingCount", "Most following"),
    ("", "Relevance"),
]
_API_USER_SORTS = frozenset({"-followerCount", "-modelCount", ""})
_CLIENT_SORT_FIELD = {
    "_likeCount": "likeCount",
    "_collectionCount": "collectionCount",
    "_subscriptionCount": "subscriptionCount",
    "_followingCount": "followingCount",
}


def _fmt(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _avatar_url(user: dict, *, prefer: int = 90) -> str:
    avatar = user.get("avatar") if isinstance(user, dict) else None
    if not isinstance(avatar, dict):
        return ""
    images = avatar.get("images") or []
    if not images:
        return ""
    by_w = sorted(images, key=lambda i: abs(int(i.get("width") or 0) - prefer))
    return str((by_w[0] if by_w else {}).get("url") or "")


def _username_from_user(u: dict) -> str:
    for key in ("username",):
        raw = _fmt(u.get(key))
        if raw and "/" not in raw:
            return raw
    for key in ("modelsUrl", "likesUrl", "collectionsUrl"):
        raw = _fmt(u.get(key))
        # https://api.sketchfab.com/v3/models?user=Name  or  ?liked_by=  /  ?by=
        if "user=" in raw:
            return raw.split("user=", 1)[1].split("&")[0]
        if "liked_by=" in raw:
            return raw.split("liked_by=", 1)[1].split("&")[0]
        if "by=" in raw:
            return raw.split("by=", 1)[1].split("&")[0]
    profile = _fmt(u.get("profileUrl"))
    if "sketchfab.com/" in profile:
        return profile.rstrip("/").split("sketchfab.com/")[-1].split("/")[0]
    return ""


def _looks_like_username(q: str) -> bool:
    q = (q or "").strip().lstrip("@")
    return bool(q) and " " not in q and len(q) >= 2


class AccountTab(ft.Column):
    def __init__(self, page: ft.Page | None = None):
        super().__init__(expand=True, spacing=6)
        self._page = page
        self._user: dict = {}
        self._username = ""
        self._is_me = False
        self._section = "models"  # models | likes | collections | subs
        self._rows: list[dict] = []
        self._next_url: str | None = None
        self._busy = False
        self._grid_cols = _DEFAULT_COLS
        self._lod = ViewportLod()
        self._lod_debouncer: ScrollLodDebouncer | None = None
        self._scroll_offset = 0.0
        self._viewport_h = 800.0
        self._rect_by_uid: dict[str, dict[str, int]] = {}

        self._on_load_user = None
        self._on_load_section = None
        self._on_preview = None
        self._on_download = None
        self._on_like = None
        self._on_open_account = None
        self._on_follow = None
        self._on_open_my_collection = None
        self._on_open_browse_collection = None
        self._get_client = None
        self._on_search_users = None
        self._on_user_models_preview = None
        self._find_view_mode = "grid"
        self._find_model_cache: dict[str, list[dict]] = {}
        self._find_model_gen = 0
        self._find_client_cursor: str | None = None
        self._find_thumb_rows: dict[str, ft.Row] = {}
        self._find_following_uids: set[str] = set()
        self._find_follow_busy: set[str] = set()
        self._find_prefetch_pending: set[str] = set()
        self._find_prefetch_gen = 0
        self._find_card_w = _FIND_MIN_CARD_W
        self._me_username = ""
        self._liked_uids: set[str] = set()
        self._downloaded_uids: set[str] = set()
        self._disk_icon_by_uid: dict[str, ft.IconButton] = {}
        self._like_btns: dict[str, ft.IconButton] = {}
        self._following = False
        self._follow_busy = False
        self._hist: list[str] = []
        self._hist_idx: int = -1
        self._hist_nav = False  # True while applying back/forward (don't push)
        self._hide_nsfw = True
        self._hide_female = True
        self._hide_male = True

        self._user_field = ft.TextField(
            label="Account",
            hint_text="Username or uid (blank = you)…",
            width=220,
            dense=True,
            text_size=13,
            content_padding=ft.padding.symmetric(horizontal=12, vertical=10),
            on_submit=lambda e: self._load_clicked(),
        )
        self._back_btn = ft.IconButton(
            icon=ft.Icons.ARROW_BACK,
            tooltip="Previous account",
            disabled=True,
            on_click=lambda e: self._hist_back(),
        )
        self._fwd_btn = ft.IconButton(
            icon=ft.Icons.ARROW_FORWARD,
            tooltip="Next account",
            disabled=True,
            on_click=lambda e: self._hist_forward(),
        )
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_400)
        self._avatar = ft.Container(
            width=48,
            height=48,
            border_radius=24,
            bgcolor="#1e293b",
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            alignment=ft.alignment.center,
            content=ft.Icon(ft.Icons.PERSON, color=ft.Colors.GREY_600),
        )
        self._profile_name = ft.Text("", size=14, weight=ft.FontWeight.W_600, color="#ffffff")
        self._profile_stats = ft.Text("", size=12, color=ft.Colors.GREY_400)
        self._profile_link = ft.TextButton(
            text="",
            url=None,
            style=ft.ButtonStyle(padding=0, color="#38bdf8"),
            visible=False,
        )
        self._follow_btn = ft.OutlinedButton(
            "Follow",
            icon=ft.Icons.PERSON_ADD,
            height=34,
            visible=False,
            on_click=lambda e: self._toggle_follow(),
        )
        self._profile_bar = ft.Row(
            [
                self._avatar,
                ft.Column(
                    [self._profile_name, self._profile_stats, self._profile_link],
                    spacing=2,
                    expand=True,
                    tight=True,
                ),
                self._follow_btn,
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            visible=False,
        )
        self._section_seg = ft.SegmentedButton(
            selected={"models"},
            segments=[
                ft.Segment(value="models", label=ft.Text("Models"), icon=ft.Icon(ft.Icons.VIEW_IN_AR)),
                ft.Segment(value="likes", label=ft.Text("Likes"), icon=ft.Icon(ft.Icons.FAVORITE_BORDER)),
                ft.Segment(value="collections", label=ft.Text("Collections"), icon=ft.Icon(ft.Icons.FOLDER_OPEN)),
                ft.Segment(value="subs", label=ft.Text("Subscribed"), icon=ft.Icon(ft.Icons.SUBSCRIPTIONS)),
                ft.Segment(value="find", label=ft.Text("Find"), icon=ft.Icon(ft.Icons.PEOPLE)),
            ],
            on_change=self._on_section_change,
        )
        self._cols_slider = ft.Slider(
            min=float(_MIN_COLS),
            max=float(_MAX_COLS),
            divisions=_MAX_COLS - _MIN_COLS,
            value=float(_DEFAULT_COLS),
            width=110,
            height=28,
            label="{value}/row",
            on_change=self._on_cols_change,
        )
        self._load_more_btn = ft.OutlinedButton(
            "Load more",
            icon=ft.Icons.EXPAND_MORE,
            on_click=lambda e: self._load_more(),
            disabled=True,
        )
        _dd = dict(dense=True, text_size=12, content_padding=ft.padding.symmetric(horizontal=8, vertical=4))
        self._find_query = ft.TextField(
            label="Find users",
            hint_text="@username or keyword (blank = top followers/models)…",
            width=260,
            dense=True,
            text_size=13,
            content_padding=ft.padding.symmetric(horizontal=12, vertical=10),
            on_submit=lambda e: self._run_user_find(append=False),
        )
        self._find_sort_dd = ft.Dropdown(
            label="Sort by",
            width=190,
            value="-followerCount",
            options=[ft.dropdown.Option(v, t) for v, t in _USER_FIND_SORTS],
            **_dd,
        )
        self._find_min = ft.TextField(
            label="Min",
            hint_text="0",
            width=72,
            dense=True,
            text_size=12,
            keyboard_type=ft.KeyboardType.NUMBER,
            tooltip="Minimum value for the sorted stat (followers, models, likes, …)",
            content_padding=ft.padding.symmetric(horizontal=8, vertical=8),
        )
        self._find_btn = ft.ElevatedButton(
            "Search",
            icon=ft.Icons.MANAGE_SEARCH,
            height=40,
            on_click=lambda e: self._run_user_find(append=False),
        )
        self._find_view_seg = ft.SegmentedButton(
            selected={"grid"},
            segments=[
                ft.Segment(value="grid", label=ft.Text("Grid"), icon=ft.Icon(ft.Icons.GRID_VIEW)),
                ft.Segment(value="list", label=ft.Text("List"), icon=ft.Icon(ft.Icons.VIEW_LIST)),
            ],
            on_change=self._on_find_view_change,
        )
        self._find_panel = ft.Container(
            visible=False,
            padding=ft.padding.only(bottom=4),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._find_query,
                            self._find_sort_dd,
                            self._find_min,
                            self._find_btn,
                            self._find_view_seg,
                        ],
                        spacing=8,
                        wrap=False,
                        scroll=ft.ScrollMode.AUTO,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "Most likes/collections: optional @username to look someone up; "
                        "blank ranks within popular accounts (Sketchfab has no global top-by-likes list).",
                        size=10,
                        color=ft.Colors.GREY_500,
                    ),
                ],
                spacing=4,
                tight=True,
            ),
        )

        self.scroller = ft.ListView(expand=True, spacing=4, padding=4, on_scroll_interval=40)
        self._scroller_wrap = wrap_middle_drag_scroll(
            self.scroller,
            expand=True,
            on_scroll=self._on_scroll,
            on_offset=self._on_autoscroll_offset,
        )
        self._lod_debouncer = ScrollLodDebouncer(self._lod, self, wait_s=0.02, throttle_s=0.012)

        self.controls = [
            ft.Container(
                padding=ft.padding.symmetric(vertical=4),
                content=ft.Row(
                    [
                        self._back_btn,
                        self._fwd_btn,
                        self._user_field,
                        ft.ElevatedButton("Load", icon=ft.Icons.PERSON_SEARCH, height=40, on_click=lambda e: self._load_clicked()),
                        ft.OutlinedButton("Me", height=40, on_click=lambda e: self._load_me()),
                        ft.Container(expand=True),
                        ft.Text("Cols", size=11, color=ft.Colors.GREY_500),
                        self._cols_slider,
                        self._status,
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ),
            self._profile_bar,
            self._find_panel,
            ft.Row(
                [self._section_seg, self._load_more_btn],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            self._scroller_wrap,
        ]
        self._show_placeholder("Load your account, look up a username, or open Find to browse users by followers/models/likes.")
        self._paint_hist_btns()

    def set_page(self, page: ft.Page) -> None:
        self._page = page

    def set_callbacks(
        self,
        *,
        on_load_user=None,
        on_load_section=None,
        on_preview=None,
        on_download=None,
        on_like=None,
        on_open_account=None,
        on_follow=None,
        on_open_my_collection=None,
        on_open_browse_collection=None,
        get_client=None,
        on_search_users=None,
        on_user_models_preview=None,
    ) -> None:
        self._on_load_user = on_load_user
        self._on_load_section = on_load_section
        self._on_preview = on_preview
        self._on_download = on_download
        self._on_like = on_like
        self._on_open_account = on_open_account
        self._on_follow = on_follow
        self._on_open_my_collection = on_open_my_collection
        self._on_open_browse_collection = on_open_browse_collection
        self._get_client = get_client
        self._on_search_users = on_search_users
        self._on_user_models_preview = on_user_models_preview

    def set_liked_uids(self, uids: set[str]) -> None:
        self._liked_uids = set(uids or set())
        if self._rows and self._section in ("models", "likes"):
            self._render()
            try:
                self.update()
            except Exception:
                pass

    def set_downloaded_uids(self, uids: set[str]) -> None:
        self._downloaded_uids = {str(u).strip().lower() for u in (uids or ()) if str(u).strip()}

    def set_content_filters(self, hide_nsfw: bool = False, hide_female: bool = False, hide_male: bool = False) -> None:
        self._hide_nsfw = bool(hide_nsfw)
        self._hide_female = bool(hide_female)
        self._hide_male = bool(hide_male)
        if self._rows and self._section in ("models", "likes", "collections", "subs"):
            self._render()

    def paint_downloaded(self, uid: str, *, on_disk: bool = True) -> None:
        uid = str(uid or "").strip().lower()
        if not uid:
            return
        if on_disk:
            self._downloaded_uids.add(uid)
        else:
            self._downloaded_uids.discard(uid)
        btn = self._disk_icon_by_uid.get(uid)
        if btn is None:
            return
        btn.icon_color = ft.Colors.GREEN_300 if on_disk else "#94a3b8"
        btn.disabled = not on_disk
        btn.tooltip = "On disk — open folder" if on_disk else "Not on disk"
        try:
            if getattr(btn, "page", None):
                btn.update()
        except Exception:
            pass

    def _paint_hist_btns(self) -> None:
        self._back_btn.disabled = self._hist_idx <= 0
        self._fwd_btn.disabled = self._hist_idx < 0 or self._hist_idx >= len(self._hist) - 1
        try:
            self._back_btn.update()
            self._fwd_btn.update()
        except Exception:
            pass

    def _push_hist(self, username: str) -> None:
        key = (username or "").strip()
        if not key or self._hist_nav:
            return
        if self._hist_idx >= 0 and self._hist_idx < len(self._hist) - 1:
            self._hist = self._hist[: self._hist_idx + 1]
        if self._hist and self._hist[-1].casefold() == key.casefold():
            self._hist_idx = len(self._hist) - 1
            self._paint_hist_btns()
            return
        self._hist.append(key)
        if len(self._hist) > 40:
            self._hist = self._hist[-40:]
        self._hist_idx = len(self._hist) - 1
        self._paint_hist_btns()

    def _hist_back(self) -> None:
        if self._hist_idx <= 0:
            return
        self._hist_idx -= 1
        self._hist_nav = True
        try:
            self._user_field.value = self._hist[self._hist_idx]
            try:
                self._user_field.update()
            except Exception:
                pass
            self._load_clicked()
        finally:
            self._hist_nav = False
            self._paint_hist_btns()

    def _hist_forward(self) -> None:
        if self._hist_idx < 0 or self._hist_idx >= len(self._hist) - 1:
            return
        self._hist_idx += 1
        self._hist_nav = True
        try:
            self._user_field.value = self._hist[self._hist_idx]
            try:
                self._user_field.update()
            except Exception:
                pass
            self._load_clicked()
        finally:
            self._hist_nav = False
            self._paint_hist_btns()

    def mark_liked(self, uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid:
            return
        self._liked_uids.add(uid)
        btn = self._like_btns.get(uid)
        if btn is None:
            return
        btn.icon = ft.Icons.FAVORITE
        btn.icon_color = "#f9a8d4"
        btn.tooltip = "Liked"
        btn.disabled = True
        try:
            if getattr(btn, "page", None):
                btn.update()
        except Exception:
            pass

    def mark_unliked(self, uid: str) -> None:
        uid = str(uid or "").strip()
        if not uid:
            return
        self._liked_uids.discard(uid)
        btn = self._like_btns.get(uid)
        if btn is None:
            return
        btn.icon = ft.Icons.FAVORITE_BORDER
        btn.icon_color = None
        btn.tooltip = "Like"
        btn.disabled = False
        try:
            if getattr(btn, "page", None):
                btn.update()
        except Exception:
            pass

    def set_me_username(self, username: str) -> None:
        self._me_username = (username or "").strip()
        if self._me_username and not (self._user_field.value or "").strip():
            self._user_field.hint_text = f"Blank = you ({self._me_username})"

    def open_user(self, username_or_key: str) -> None:
        """Load a user into this tab (Account field + sections/filters stay usable)."""
        key = (username_or_key or "").strip()
        if not key:
            return
        self._user_field.value = key
        try:
            self._user_field.update()
        except Exception:
            pass
        self._load_clicked()

    def scroll_to_top(self) -> None:
        self._scroll_offset = 0.0
        try:
            self.scroller.scroll_to(offset=0, duration=200)
        except Exception:
            pass

    def on_tab_shown(self) -> None:
        if not self._user and self._me_username and not (self._user_field.value or "").strip():
            self._load_me()
            return
        off = self._scroll_offset
        if not self._rows or off <= 0:
            return
        # Hide briefly so Flet's tab remount at y=0 is not visible, then jump.
        self.scroller.opacity = 0
        try:
            self.scroller.update()
        except Exception:
            pass

        async def _restore():
            try:
                self.scroller.scroll_to(offset=off, duration=0)
            except Exception:
                pass
            self.scroller.opacity = 1
            try:
                self.scroller.update()
            except Exception:
                pass
            if self._lod_debouncer and self._page:
                self._lod_debouncer.kick(
                    self._page,
                    scroll=off,
                    viewport=self._viewport_h,
                    remount_delay=0.05,
                )

        if self._page:
            self._page.run_task(_restore)
        else:
            self.scroller.opacity = 1

    def _show_placeholder(self, msg: str) -> None:
        self.scroller.controls = [
            ft.Container(padding=24, content=ft.Text(msg, size=13, color=ft.Colors.GREY_500))
        ]

    def _on_cols_change(self, e) -> None:
        try:
            self._grid_cols = int(float(e.control.value))
        except (TypeError, ValueError):
            return
        if self._section == "find" and self._rows:
            self._render_find_results()
            self.update()
        elif self._rows:
            self._render()
            self.update()

    def _on_find_view_change(self, e) -> None:
        sel = getattr(e.control, "selected", None) or {"grid"}
        if isinstance(sel, set) and sel:
            self._find_view_mode = next(iter(sel))
        if self._section == "find" and self._rows:
            self._render_find_results()
            self.update()

    def _on_section_change(self, e) -> None:
        sel = getattr(e.control, "selected", None) or {"models"}
        if isinstance(sel, set) and sel:
            self._section = next(iter(sel))
        self._find_panel.visible = self._section == "find"
        self._rows = []
        self._next_url = None
        if self._section == "find":
            self._profile_bar.visible = bool(self._user)
            if self._grid_cols > 4:
                self._grid_cols = _FIND_DEFAULT_COLS
                self._cols_slider.value = float(_FIND_DEFAULT_COLS)
            if not self._rows:
                self._show_placeholder(
                    "Find users — pick a sort, optional @username or keyword, then Search. "
                    "Grid shows 3 recent models per user."
                )
            self.update()
            return
        self._reload_section(append=False)

    def _load_me(self) -> None:
        self._user_field.value = self._me_username or ""
        try:
            self._user_field.update()
        except Exception:
            pass
        self._load_clicked()

    def _load_clicked(self) -> None:
        if self._busy:
            return
        want = (self._user_field.value or "").strip() or self._me_username
        if not want:
            self._status.value = "Enter a username or click Me."
            self.update()
            return
        if not self._on_load_user:
            return
        self._busy = True
        self._status.value = f"Loading {want}…"
        self.update()
        page = self._page

        def work():
            err = None
            user = None
            try:
                user = self._on_load_user(want)
            except Exception as e:
                err = str(e)

            async def finish():
                self._busy = False
                if err or not user:
                    self._status.value = err or "User not found"
                    self._show_placeholder(self._status.value)
                    self.update()
                    return
                self._apply_user(user)
                self._push_hist(self._username)
                self._reload_section(append=False)

            if page is not None:
                page.run_task(finish)
            else:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def _apply_user(self, user: dict) -> None:
        self._user = user or {}
        self._username = _username_from_user(self._user) or (self._user_field.value or "").strip()
        self._is_me = bool(self._me_username) and self._username.casefold() == self._me_username.casefold()
        dn = _fmt(user.get("displayName")) or self._username
        self._profile_name.value = f"{dn}  ·  @{self._username}"
        self._profile_stats.value = (
            f"Models {int(user.get('modelCount') or 0):,}  ·  "
            f"Likes {int(user.get('likeCount') or 0):,}  ·  "
            f"Collections {int(user.get('collectionCount') or 0):,}  ·  "
            f"Subs {int(user.get('subscriptionCount') or 0):,}  ·  "
            f"Followers {int(user.get('followerCount') or 0):,}"
        )
        url = _fmt(user.get("profileUrl"))
        if url:
            self._profile_link.text = url
            self._profile_link.url = url
            self._profile_link.visible = True
        else:
            self._profile_link.text = ""
            self._profile_link.url = None
            self._profile_link.visible = False
        av = _avatar_url(user)
        if av:
            self._avatar.content = ft.Image(src=av, width=48, height=48, fit=ft.ImageFit.COVER)
        else:
            self._avatar.content = ft.Icon(ft.Icons.PERSON, color=ft.Colors.GREY_600)
        self._profile_bar.visible = True
        self._follow_btn.visible = not self._is_me
        self._follow_btn.disabled = self._is_me or self._follow_busy
        self._paint_follow_btn()
        self._status.value = "Ready"
        if not self._is_me and self._section == "subs":
            self._section = "models"
            self._section_seg.selected = {"models"}
        if not self._is_me and self._on_follow:
            self._refresh_follow_status()

    def _paint_follow_btn(self) -> None:
        if self._following:
            self._follow_btn.text = "Unfollow"
            self._follow_btn.icon = ft.Icons.PERSON_REMOVE
        else:
            self._follow_btn.text = "Follow"
            self._follow_btn.icon = ft.Icons.PERSON_ADD

    def _refresh_follow_status(self) -> None:
        uid = _fmt(self._user.get("uid"))
        if not uid or not self._on_follow or self._is_me:
            return
        page = self._page

        def work():
            following = False
            try:
                following = bool(self._on_follow("status", uid))
            except Exception:
                following = False

            async def finish():
                self._following = following
                self._paint_follow_btn()
                try:
                    self._follow_btn.update()
                except Exception:
                    self.update()

            if page:
                page.run_task(finish)

        threading.Thread(target=work, daemon=True).start()

    def _toggle_follow(self) -> None:
        uid = _fmt(self._user.get("uid"))
        if not uid or self._is_me or self._follow_busy or not self._on_follow:
            return
        want = not self._following
        self._follow_busy = True
        self._follow_btn.disabled = True
        self._follow_btn.text = "…"
        try:
            self._follow_btn.update()
        except Exception:
            pass
        page = self._page

        def work():
            err = None
            try:
                self._on_follow("follow" if want else "unfollow", uid)
            except Exception as e:
                err = str(e)

            async def finish():
                self._follow_busy = False
                self._follow_btn.disabled = False
                if err:
                    already = "already" in err.lower() or "400" in err
                    if already:
                        self._following = want
                        self._status.value = "Following" if want else "Unfollowed"
                    else:
                        self._status.value = f"Follow failed: {err}"
                else:
                    self._following = want
                    self._status.value = "Following" if want else "Unfollowed"
                self._paint_follow_btn()
                self.update()

            if page:
                page.run_task(finish)

        threading.Thread(target=work, daemon=True).start()

    def _find_min_value(self) -> int:
        try:
            return max(0, int(float((self._find_min.value or "0").strip() or 0)))
        except (TypeError, ValueError):
            return 0

    def _open_find_user(self, username: str) -> None:
        key = (username or "").strip()
        if not key:
            return
        self._section = "models"
        self._section_seg.selected = {"models"}
        self._find_panel.visible = False
        self.open_user(key)

    def _run_user_find(self, *, append: bool) -> None:
        if self._section != "find" or self._busy:
            return
        search_fn = self._on_search_users
        if not search_fn:
            self._status.value = "User search not wired."
            self.update()
            return
        sort_key = (self._find_sort_dd.value or "-followerCount").strip()
        query = (self._find_query.value or "").strip().lstrip("@")
        min_val = self._find_min_value()

        self._busy = True
        self._status.value = "Searching users…"
        self._load_more_btn.disabled = True
        if not append:
            self._rows = []
            self._next_url = None
            self._find_client_cursor = None
            self._find_model_cache.clear()
            self._find_model_gen += 1
            self._find_prefetch_pending.clear()
            self._find_thumb_rows.clear()
        self.update()
        cursor = self._next_url if append else None
        client_cursor = self._find_client_cursor if append else None
        page = self._page

        def work():
            err = None
            users: list[dict] = []
            next_url = None
            next_client = None
            try:
                if sort_key in _API_USER_SORTS:
                    data = search_fn(query=query, sort_by=sort_key, cursor_url=cursor)
                    users = list(data.get("results") or [])
                    next_url = data.get("next")
                    stat_field = {
                        "-followerCount": "followerCount",
                        "-modelCount": "modelCount",
                    }.get(sort_key)
                    if min_val and stat_field:
                        users = [u for u in users if int(u.get(stat_field) or 0) >= min_val]
                else:
                    field = _CLIENT_SORT_FIELD[sort_key]
                    merged: list[dict] = []
                    seen: set[str] = set()
                    if append:
                        for u in self._rows:
                            uid = str(u.get("uid") or "")
                            if uid:
                                seen.add(uid)
                                merged.append(u)

                    # Exact @username lookup (not a genre search).
                    if query and _looks_like_username(query) and self._on_load_user and not append:
                        try:
                            hit = self._on_load_user(query)
                            uid = str(hit.get("uid") or "")
                            if uid and uid not in seen:
                                seen.add(uid)
                                merged.append(hit)
                        except Exception:
                            pass

                    fetch_q = query if query and not _looks_like_username(query) else ""
                    api_sort = "-followerCount" if not fetch_q else ""
                    url = client_cursor
                    pages = 2 if append else (8 if not fetch_q else 6)
                    for _ in range(pages):
                        data = search_fn(query=fetch_q, sort_by=api_sort, cursor_url=url)
                        for u in data.get("results") or []:
                            uid = str(u.get("uid") or "")
                            if uid and uid not in seen:
                                seen.add(uid)
                                merged.append(u)
                        url = data.get("next")
                        next_client = url
                        if not url:
                            break

                    if fetch_q:
                        # Keyword search: also pull username hits when query looks like a name.
                        if _looks_like_username(query):
                            data2 = search_fn(query=query, sort_by="", cursor_url=None)
                            for u in data2.get("results") or []:
                                uid = str(u.get("uid") or "")
                                if uid and uid not in seen:
                                    seen.add(uid)
                                    merged.append(u)

                    merged.sort(key=lambda u: int(u.get(field) or 0), reverse=True)
                    if min_val:
                        merged = [u for u in merged if int(u.get(field) or 0) >= min_val]
                    users = merged if append else merged
            except Exception as e:
                err = str(e)

            async def finish():
                self._busy = False
                if err:
                    self._status.value = err
                    if not append:
                        self._show_placeholder(err)
                    self.update()
                    return
                if append and sort_key in _API_USER_SORTS:
                    self._rows.extend(users)
                elif append and sort_key in _CLIENT_SORT_FIELD:
                    self._rows = users
                else:
                    self._rows = users
                if sort_key in _API_USER_SORTS:
                    self._next_url = next_url
                    self._find_client_cursor = None
                    self._load_more_btn.disabled = not bool(self._next_url)
                else:
                    self._next_url = None
                    self._find_client_cursor = next_client
                    self._load_more_btn.disabled = not bool(next_client)
                n = len(self._rows)
                if not n:
                    self._status.value = "No users matched"
                    self._show_placeholder("No users matched — try @username, another keyword, or lower Min.")
                else:
                    extra = ""
                    if self._next_url or self._find_client_cursor:
                        extra = " · load more"
                    if sort_key in _CLIENT_SORT_FIELD and not query:
                        extra += " · ranked in popular-user sample"
                    self._status.value = f"{n:,} user(s){extra}"
                    self._render_find_results()
                    self._load_find_followings()
                self.update()

            if page:
                page.run_task(finish)
            else:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def _find_cols_n(self) -> int:
        cols_n = max(1, min(4, int(self._grid_cols or _FIND_DEFAULT_COLS)))
        avail = _FALLBACK_GRID_AVAIL
        if self._page is not None:
            try:
                pw = int(float(self._page.width or 0))
                if pw >= 1000:
                    avail = max(520, pw - _GRID_SIDE_CHROME)
            except (TypeError, ValueError):
                pass
        while cols_n > 1 and (avail - _GRID_GAP * (cols_n - 1)) / cols_n < _FIND_MIN_CARD_W:
            cols_n -= 1
        return cols_n

    def _find_card_dims(self) -> tuple[int, int]:
        cols_n = self._find_cols_n()
        avail = _FALLBACK_GRID_AVAIL
        if self._page is not None:
            try:
                pw = int(float(self._page.width or 0))
                if pw >= 1000:
                    avail = max(520, pw - _GRID_SIDE_CHROME)
            except (TypeError, ValueError):
                pass
        card_w = int((avail - _GRID_GAP * max(0, cols_n - 1)) / cols_n)
        card_w = max(_FIND_MIN_CARD_W, card_w)
        self._find_card_w = card_w
        return card_w, _FIND_THUMB

    def _find_prefetch_indices(self) -> list[int]:
        if not self._rows:
            return []
        cols = self._find_cols_n()
        rh = float(_FIND_CARD_ROW_H)
        first_row = max(0, int(self._scroll_offset // rh) - 1)
        vis_rows = max(3, int(self._viewport_h // rh) + 4)
        last_row = first_row + vis_rows
        i0 = first_row * cols
        i1 = min(len(self._rows), last_row * cols)
        if len(self._rows) <= cols * 6:
            return list(range(len(self._rows)))
        return list(range(i0, i1))

    def _find_thumb_cells(self, uname: str, *, card_w: int) -> list[ft.Control]:
        models = self._find_model_cache.get(uname) or []
        thumb_w = max(48, (card_w - 16 - 8) // 3)
        cells: list[ft.Control] = []
        for i in range(3):
            if i < len(models):
                m = models[i]
                low = _fmt(m.get("Thumbnail"))
                # Find previews: low-res only — faster, avoids huge HD stalls.
                box, _img = make_lod_thumb(low, low, size=thumb_w)
                box.width = thumb_w
                box.height = _FIND_THUMB
                cells.append(box)
            else:
                cells.append(
                    ft.Container(
                        width=thumb_w,
                        height=_FIND_THUMB,
                        bgcolor="#1e293b",
                        border_radius=6,
                        alignment=ft.alignment.center,
                        content=ft.ProgressRing(width=16, height=16, stroke_width=2)
                        if uname in self._find_prefetch_pending
                        else ft.Icon(ft.Icons.IMAGE_NOT_SUPPORTED, size=18, color=ft.Colors.GREY_700),
                    )
                )
        return cells

    def _find_model_thumbs(self, uname: str, *, card_w: int) -> ft.Row:
        row = ft.Row(self._find_thumb_cells(uname, card_w=card_w), spacing=4, wrap=False)
        if uname:
            self._find_thumb_rows[uname] = row
        return row

    def _patch_find_thumbs(self, usernames: list[str]) -> None:
        if self._section != "find":
            return
        card_w = self._find_card_w or self._find_card_dims()[0]
        for un in usernames:
            row = self._find_thumb_rows.get(un)
            if row is None:
                continue
            row.controls = self._find_thumb_cells(un, card_w=card_w)
            try:
                row.update()
            except Exception:
                pass

    def _find_follow_control(self, user: dict) -> ft.Control:
        uid = str(user.get("uid") or "").strip()
        uname = _username_from_user(user)
        if not uid or not self._on_follow:
            return ft.Container(width=0)
        if self._me_username and uname.casefold() == self._me_username.casefold():
            return ft.Container(width=0)
        following = uid.casefold() in self._find_following_uids
        busy = uid in self._find_follow_busy
        return ft.IconButton(
            icon=ft.Icons.PERSON_REMOVE if following else ft.Icons.PERSON_ADD,
            icon_size=18,
            icon_color="#f9a8d4" if following else "#94a3b8",
            tooltip="Unfollow" if following else "Follow",
            disabled=busy,
            style=ft.ButtonStyle(padding=0),
            on_click=lambda e, u=uid, un=uname: self._toggle_find_follow(u, un),
        )

    def _load_find_followings(self) -> None:
        if not self._on_follow:
            return
        page = self._page

        def work():
            uids: set[str] = set()
            try:
                from sketchfab_client import SketchfabClient

                raw = SketchfabClient().load_my_following_uids()
                uids = {str(u).casefold() for u in raw if u}
            except Exception:
                uids = set()

            async def finish():
                self._find_following_uids = uids
                if self._section == "find" and self._rows:
                    self._render_find_results(schedule_prefetch=False)
                    try:
                        self.update()
                    except Exception:
                        pass

            if page:
                page.run_task(finish)

        threading.Thread(target=work, daemon=True).start()

    def _toggle_find_follow(self, user_uid: str, username: str) -> None:
        uid = str(user_uid or "").strip()
        if not uid or not self._on_follow or uid in self._find_follow_busy:
            return
        want = uid.casefold() not in self._find_following_uids
        self._find_follow_busy.add(uid)
        page = self._page

        def work():
            err = None
            try:
                self._on_follow("follow" if want else "unfollow", uid)
            except Exception as e:
                err = str(e)

            async def finish():
                self._find_follow_busy.discard(uid)
                if err:
                    already = "already" in err.lower()
                    if not already:
                        self._status.value = f"Follow failed: {err}"
                    elif want:
                        self._find_following_uids.add(uid.casefold())
                else:
                    if want:
                        self._find_following_uids.add(uid.casefold())
                    else:
                        self._find_following_uids.discard(uid.casefold())
                if self._section == "find":
                    self._render_find_results(schedule_prefetch=False)
                try:
                    self.update()
                except Exception:
                    pass

            if page:
                page.run_task(finish)

        threading.Thread(target=work, daemon=True).start()

    def _schedule_find_prefetch(self) -> None:
        if self._section != "find" or not self._rows:
            return
        if downloads_busy():
            return
        indices = self._find_prefetch_indices()
        usernames: list[str] = []
        cols = self._find_cols_n()

        def row_key(i: int) -> tuple[int, int]:
            return (i // cols, i % cols)

        for i in sorted(set(indices), key=row_key):
            if 0 <= i < len(self._rows):
                un = _username_from_user(self._rows[i])
                if un and un not in self._find_model_cache and un not in self._find_prefetch_pending:
                    usernames.append(un)
        if usernames:
            self._prefetch_find_models(usernames)

    def _prefetch_find_models(self, usernames: list[str]) -> None:
        fn = self._on_user_models_preview
        if not fn or not usernames:
            return
        gen = self._find_model_gen
        self._find_prefetch_gen += 1
        batch_gen = self._find_prefetch_gen
        page = self._page
        todo = [u for u in usernames if u and u not in self._find_model_cache]
        for u in todo:
            self._find_prefetch_pending.add(u)
        if not todo:
            return
        card_w = self._find_card_w or self._find_card_dims()[0]

        def work():
            pending_ui: list[str] = []
            last_flush = time.monotonic()

            def flush():
                nonlocal pending_ui, last_flush
                if not pending_ui or batch_gen != self._find_prefetch_gen:
                    pending_ui = []
                    return
                names = pending_ui[:]
                pending_ui = []
                last_flush = time.monotonic()

                async def tick():
                    if batch_gen != self._find_prefetch_gen or gen != self._find_model_gen:
                        return
                    self._patch_find_thumbs(names)

                if page:
                    page.run_task(tick)

            with ThreadPoolExecutor(max_workers=_FIND_PREFETCH_WORKERS, thread_name_prefix="find-m") as pool:
                futs = {pool.submit(fn, u, 3): u for u in todo}
                for fut in as_completed(futs):
                    if gen != self._find_model_gen or batch_gen != self._find_prefetch_gen:
                        return
                    un = futs[fut]
                    self._find_prefetch_pending.discard(un)
                    try:
                        self._find_model_cache[un] = list(fut.result() or [])
                    except Exception:
                        self._find_model_cache[un] = []
                    pending_ui.append(un)
                    now = time.monotonic()
                    if len(pending_ui) >= 4 or (now - last_flush) >= 0.12:
                        flush()
            flush()

        threading.Thread(target=work, daemon=True).start()

    def _find_user_header(self, user: dict) -> tuple[str, str, str, ft.Control]:
        uname = _username_from_user(user)
        dn = _fmt(user.get("displayName")) or uname
        av = _avatar_url(user, prefer=48)
        stats = (
            f"Models {int(user.get('modelCount') or 0):,} · "
            f"Likes {int(user.get('likeCount') or 0):,} · "
            f"Followers {int(user.get('followerCount') or 0):,}"
        )
        avatar = (
            ft.Image(src=av, width=44, height=44, fit=ft.ImageFit.COVER, border_radius=22)
            if av
            else ft.Icon(ft.Icons.PERSON, size=36, color=ft.Colors.GREY_600)
        )
        return uname, dn, stats, avatar

    def _find_user_card(self, user: dict, idx: int) -> ft.Container:
        uname, dn, stats, avatar = self._find_user_header(user)
        card_w, _ = self._find_card_dims()
        thumb_row = self._find_model_thumbs(uname, card_w=card_w)
        follow = self._find_follow_control(user)
        header = ft.Row(
            [
                ft.Container(width=48, content=avatar),
                ft.Column(
                    [
                        ft.Text(dn, size=12, weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(f"@{uname}", size=10, color=_META_COLOR, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(stats, size=9, color=ft.Colors.GREY_400, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ],
                    spacing=1,
                    expand=True,
                    tight=True,
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )
        return ft.Container(
            width=card_w,
            bgcolor="#111827",
            border_radius=8,
            padding=8,
            tooltip=f"Open @{uname}",
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.GestureDetector(
                                content=header,
                                on_tap=lambda e, u=uname: self._open_find_user(u),
                            ),
                            follow,
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    thumb_row,
                ],
                spacing=6,
                tight=True,
            ),
        )

    def _render_find_results(self, *, schedule_prefetch: bool = True) -> None:
        self._find_thumb_rows.clear()
        if self._find_view_mode == "list":
            self.scroller.controls = [
                self._find_user_row(u, i) for i, u in enumerate(self._rows)
            ]
        else:
            card_w, _ = self._find_card_dims()
            cols_n = self._find_cols_n()
            cards = [self._find_user_card(u, i) for i, u in enumerate(self._rows)]
            grid_rows: list[ft.Control] = []
            for i in range(0, len(cards), cols_n):
                grid_rows.append(
                    ft.Container(
                        padding=ft.padding.only(bottom=6),
                        content=ft.Row(cards[i : i + cols_n], spacing=_GRID_GAP, wrap=False),
                    )
                )
            self.scroller.controls = grid_rows
        if schedule_prefetch:
            self._schedule_find_prefetch()

    def _find_user_row(self, user: dict, idx: int) -> ft.Container:
        uname, dn, stats, avatar = self._find_user_header(user)
        card_w = max(220, self._find_card_w)
        thumb_row = self._find_model_thumbs(uname, card_w=card_w)
        follow = self._find_follow_control(user)
        body = ft.Row(
            [
                ft.Container(width=48, content=avatar),
                ft.Column(
                    [
                        ft.Text(dn, size=13, weight=ft.FontWeight.W_600, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(f"@{uname}", size=11, color=_META_COLOR, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(stats, size=10, color=ft.Colors.GREY_400, max_lines=1),
                        thumb_row,
                    ],
                    spacing=2,
                    expand=True,
                    tight=True,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )
        return ft.Container(
            bgcolor="#111827" if idx % 2 else None,
            border_radius=8,
            padding=8,
            tooltip=f"Open @{uname}",
            content=ft.Row(
                [
                    ft.GestureDetector(
                        content=body,
                        on_tap=lambda e, u=uname: self._open_find_user(u),
                        expand=True,
                    ),
                    follow,
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=ft.Colors.GREY_500),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _reload_section(self, *, append: bool) -> None:
        if self._section == "find":
            self._run_user_find(append=append)
            return
        if self._busy or not self._username or not self._on_load_section:
            return
        if self._section == "subs" and not self._is_me:
            self._show_placeholder("Subscribed collections are only available for your own account (API /me/subscriptions).")
            self._status.value = "Subscribed = me only"
            self.update()
            return
        self._busy = True
        self._status.value = f"Loading {self._section}…"
        self._load_more_btn.disabled = True
        self.update()
        cursor = self._next_url if append else None
        section = self._section
        username = self._username

        def work():
            err = None
            data = None
            try:
                data = self._on_load_section(section, username, cursor)
            except Exception as e:
                err = str(e)

            async def finish():
                self._busy = False
                if err or data is None:
                    self._status.value = err or "Load failed"
                    if not append:
                        self._show_placeholder(self._status.value)
                    self.update()
                    return
                results = data.get("results") or []
                self._next_url = data.get("next")
                self._load_more_btn.disabled = not bool(self._next_url)
                if section in ("models", "likes"):
                    rows = [normalize_search_model(m) for m in results]
                else:
                    rows = [self._normalize_collection(c) for c in results]
                if append:
                    self._rows.extend(rows)
                else:
                    self._rows = rows
                shown = len(self._rows)
                if not shown and not append:
                    mc = int(self._user.get("modelCount") or 0)
                    cc = int(self._user.get("collectionCount") or 0)
                    if section == "models" and mc == 0:
                        self._status.value = "No public models"
                        self._show_placeholder(
                            "No models listed for this account. If the website profile is gone, "
                            "Sketchfab may have deleted the user — liked/downloadable models can "
                            "still work via UID on Liked / Browse. Open a collection by UID if you have one."
                        )
                    elif section == "collections" and cc == 0:
                        self._status.value = "No collections"
                        self._show_placeholder(
                            "No collections owned by this username. Deleted accounts often still "
                            "appear in search but return empty/wrong lists — use Browse Collections "
                            "with a collection name, or open from a model you already liked."
                        )
                    elif section == "collections" and cc > 0:
                        self._status.value = f"Collections count {cc:,} but API returned none"
                        self._show_placeholder(
                            f"Profile reports {cc:,} collection(s), but Sketchfab’s list API "
                            f"returned none for @{username}. Try Browse Collections with their "
                            "username, or open a collection from one of their models."
                        )
                    else:
                        self._status.value = f"No {section}"
                        self._show_placeholder(f"No {section} for @{username}.")
                else:
                    self._status.value = f"{shown:,} loaded" + (" · more available" if self._next_url else "")
                    self._render()
                    if section in ("collections", "subs"):
                        self._enrich_collection_thumbs()
                self.update()

            if self._page:
                self._page.run_task(finish)
            else:
                # fallback
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def _enrich_collection_thumbs(self) -> None:
        """Collection list payloads often omit thumbs — pull first model image."""
        need = [r for r in self._rows if r.get("UID") and not str(r.get("Thumbnail") or "").strip()]
        if not need or not self._get_client:
            return

        def work():
            from browse_collections import unwrap_collection_model
            from browse_models import thumb_urls_from_api

            try:
                client = self._get_client()
            except Exception:
                return
            changed = False
            for row in need:
                uid = str(row.get("UID") or "")
                if not uid or str(row.get("Thumbnail") or "").strip():
                    continue
                try:
                    data = client.list_collection_models(uid, count=1)
                    results = data.get("results") or []
                    if not results:
                        continue
                    model = unwrap_collection_model(results[0])
                    low, hi = thumb_urls_from_api(model)
                    url = low or hi
                    if not url:
                        continue
                    row["Thumbnail"] = url
                    row["Thumbnail HD"] = hi or url
                    changed = True
                except Exception:
                    continue
            if not changed:
                return

            def apply():
                if self._section in ("collections", "subs"):
                    self._render()
                    try:
                        self.update()
                    except Exception:
                        pass

            if self._page:
                async def _run():
                    apply()

                try:
                    self._page.run_task(_run)
                except Exception:
                    apply()
            else:
                apply()

        threading.Thread(target=work, daemon=True).start()

    def _normalize_collection(self, c: dict) -> dict:
        nested = c.get("collection") if isinstance(c.get("collection"), dict) else None
        if nested and nested.get("uid"):
            c = nested
        user = c.get("user") or c.get("owner") or {}
        uname = str(user.get("username") or "").strip()
        thumbs = (c.get("thumbnails") or {}).get("images") or []
        by_w = sorted(thumbs, key=lambda i: int(i.get("width") or 0))
        thumb = str(by_w[-1].get("url") or "") if by_w else ""
        return {
            "kind": "collection",
            "UID": c.get("uid") or "",
            "Name": c.get("name") or "",
            "Author": user.get("displayName") or uname or "",
            "Author Username": uname,
            "modelCount": int(c.get("modelCount") or 0),
            "Thumbnail": thumb,
            "Thumbnail HD": thumb,
            "slug": c.get("slug") or "",
            "uri": c.get("uri") or "",
        }

    def _load_more(self) -> None:
        if self._section == "find":
            if self._next_url or self._find_client_cursor:
                self._run_user_find(append=True)
            return
        if not self._next_url:
            return
        self._reload_section(append=True)

    def _on_autoscroll_offset(self, offset: float) -> None:
        self._scroll_offset = float(offset or 0)
        if self._lod_debouncer and self._page:
            self._lod_debouncer.nudge(self._page, self._scroll_offset, self._viewport_h)

    def _on_scroll(self, e: ft.OnScrollEvent) -> None:
        try:
            self._scroll_offset = float(e.pixels or 0)
            self._viewport_h = float(e.viewport_dimension or self._viewport_h or 800)
        except (TypeError, ValueError):
            pass
        if self._lod_debouncer and self._page:
            self._lod_debouncer.on_scroll(e, self._page)
        if self._section == "find" and self._rows and not self._busy:
            self._schedule_find_prefetch()
        has_more = bool(self._next_url) or (
            self._section == "find" and bool(self._find_client_cursor)
        )
        if self._busy or not has_more:
            return
        try:
            near = (self._scroll_offset + self._viewport_h) >= (float(e.max_scroll_extent or 0) - 160)
        except (TypeError, ValueError):
            near = False
        if near:
            self._load_more()

    def _card_dims(self) -> tuple[int, int]:
        page_w = 0
        if self._page is not None:
            try:
                page_w = int(float(self._page.width or 0))
            except (TypeError, ValueError):
                page_w = 0
        if page_w >= 1000:
            avail = max(520, page_w - _GRID_SIDE_CHROME)
        else:
            avail = _FALLBACK_GRID_AVAIL
        cols = max(_MIN_COLS, min(_MAX_COLS, int(self._grid_cols or _DEFAULT_COLS)))
        while cols > 1 and (avail - _GRID_GAP * (cols - 1)) / cols < _MIN_CARD_W:
            cols -= 1
        card_w = max(_MIN_CARD_W, int((avail - _GRID_GAP * (cols - 1)) / cols))
        return card_w, max(120, card_w - 4)

    def on_page_resize(self, width: float | None = None) -> None:
        if self._section in ("collections", "subs") or not self._rows:
            return
        keep = self._scroll_offset
        self._render()
        try:
            self.update()
        except Exception:
            pass
        if keep > 0:
            try:
                self.scroller.scroll_to(offset=keep, duration=0)
            except Exception:
                pass

    def on_viewport_sync(self) -> None:
        if self._lod_debouncer and self._page:
            self._lod_debouncer.sync_now(allow_downgrade=False)

    def _visible_rows(self) -> list[dict]:
        rows = list(self._rows or [])
        if self._section in ("models", "likes"):
            return filter_rows(
                rows,
                hide_nsfw=self._hide_nsfw,
                hide_female=self._hide_female,
                hide_male=self._hide_male,
            )
        if self._section in ("collections", "subs"):
            out = []
            for r in rows:
                probe = {
                    "Name": r.get("Name") or "",
                    "Tags": r.get("Name") or "",
                    "Categories": "",
                    "Assigned Collection(s)": r.get("Name") or "",
                }
                if passes_content_filter(
                    probe,
                    hide_nsfw=self._hide_nsfw,
                    hide_female=self._hide_female,
                    hide_male=self._hide_male,
                ):
                    out.append(r)
            return out
        return rows

    def _render(self) -> None:
        self._like_btns.clear()
        self._disk_icon_by_uid.clear()
        self._lod.clear()
        if not self._rows:
            self._show_placeholder("No items in this section.")
            return
        rows = self._visible_rows()
        if not rows:
            self._show_placeholder("All items hidden by N / W / M toolbar filters.")
            return
        if self._section in ("collections", "subs"):
            self.scroller.controls = [self._collection_row(r) for r in rows]
            return
        card_w, thumb_h = self._card_dims()
        cols_n = max(_MIN_COLS, min(_MAX_COLS, int(self._grid_cols or _DEFAULT_COLS)))
        avail = _FALLBACK_GRID_AVAIL
        if self._page is not None:
            try:
                pw = int(float(self._page.width or 0))
                if pw >= 1000:
                    avail = max(520, pw - _GRID_SIDE_CHROME)
            except (TypeError, ValueError):
                pass
        while cols_n > 1 and (avail - _GRID_GAP * (cols_n - 1)) / cols_n < _MIN_CARD_W:
            cols_n -= 1
        card_h = float(thumb_h)
        cards = []
        for i, r in enumerate(rows):
            row_i = i // cols_n
            y0 = row_i * (card_h + 8)
            cards.append(self._model_card(r, lod_y0=y0, lod_y1=y0 + card_h))
        grid_rows = []
        for i in range(0, len(cards), cols_n):
            grid_rows.append(
                ft.Container(
                    padding=ft.padding.only(bottom=6),
                    height=card_h + 6,
                    content=ft.Row(cards[i : i + cols_n], spacing=8, wrap=False),
                )
            )
        self.scroller.controls = grid_rows

    def _collection_row(self, row: dict) -> ft.Container:
        name = _fmt(row.get("Name"))
        count = int(row.get("modelCount") or 0)
        uid = _fmt(row.get("UID"))
        thumb = _fmt(row.get("Thumbnail"))
        slug = _fmt(row.get("slug"))
        author_key = _fmt(row.get("Author Username")) or self._username
        box, img = make_lod_thumb(thumb, thumb, size=56)
        url = collection_public_url(name, uid, slug=slug, username=author_key or None) if uid else ""
        models_label = "model" if count == 1 else "models"
        mine = bool(self._is_me) and self._section == "collections"
        # Subscribed (or someone else's collections) → Browse Collections; own → My Collections.
        open_in_app = mine or self._section == "subs" or (self._section == "collections" and not mine)

        def _open_in_app(_e=None, r=row, is_mine=mine):
            meta = {
                "UID": _fmt(r.get("UID")),
                "Name": _fmt(r.get("Name")),
                "Slug": _fmt(r.get("slug")),
                "Author": _fmt(r.get("Author")),
                "Author Username": _fmt(r.get("Author Username")) or self._username,
                "Model Count": int(r.get("modelCount") or 0),
                "Thumbnail": _fmt(r.get("Thumbnail")),
                "URL": collection_public_url(
                    _fmt(r.get("Name")),
                    _fmt(r.get("UID")),
                    slug=_fmt(r.get("slug")),
                    username=_fmt(r.get("Author Username")) or self._username or None,
                ),
            }
            if is_mine and self._on_open_my_collection:
                self._on_open_my_collection(meta)
            elif self._on_open_browse_collection:
                self._on_open_browse_collection(meta)

        title = (
            ft.TextButton(
                text=name,
                style=ft.ButtonStyle(padding=0, color="#ffffff"),
                tooltip="Open in My Collections" if mine else "Open in Browse Collections",
                on_click=_open_in_app if open_in_app else None,
            )
            if open_in_app
            else ft.Text(name, size=13, weight=ft.FontWeight.W_600, color="#ffffff")
        )
        web_btn = ft.IconButton(
            icon=ft.Icons.OPEN_IN_NEW,
            icon_size=16,
            icon_color="#67e8f9",
            tooltip="Open on Sketchfab",
            url=url or None,
            disabled=not bool(url),
            style=ft.ButtonStyle(padding=0),
        )
        return ft.Container(
            bgcolor="#111827",
            border_radius=8,
            padding=8,
            content=ft.Row(
                [
                    box,
                    ft.Column(
                        [
                            ft.Row(
                                [title, web_btn],
                                spacing=4,
                                tight=True,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Text(f"{count:,} {models_label} · {uid}", size=11, color=_META_COLOR),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _model_card(self, row: dict, *, lod_y0: float, lod_y1: float) -> ft.Container:
        card_w, thumb_h = self._card_dims()
        low = _fmt(row.get("Thumbnail"))
        hi = _fmt(row.get("Thumbnail HD")) or low
        prefer_hi = bool(hi) and lod_y0 <= (self._viewport_h or 800.0) * 3.0
        thumb_box, img = make_lod_thumb(low, hi, size=thumb_h, prefer_hi=prefer_hi)
        if img is not None:
            self._lod.add(img, low, hi, lod_y0, lod_y1, level="hi" if prefer_hi else "low")
        uid = _fmt(row.get("UID"))
        name = _fmt(row.get("Name"))
        author = _fmt(row.get("Author"))
        author_key = _fmt(row.get("Author Username")) or author
        stats = f"{int(row.get('viewCount') or 0):,} views · {int(row.get('likeCount') or 0):,} likes"
        dl = str(row.get("Downloadable", "")).lower() == "yes"
        on_disk = bool(uid) and uid.lower() in self._downloaded_uids
        liked = uid in self._liked_uids
        model_url = f"https://sketchfab.com/3d-models/{uid}" if uid else None

        def _preview(_e=None, u=uid, n=name, t=low):
            if u and self._on_preview:
                r = self._rect_by_uid.get(u)
                try:
                    self._on_preview(u, n, "", t, rect=r)
                except TypeError:
                    self._on_preview(u, n, "", t)

        def _like(_e=None, u=uid):
            if not u or not self._on_like:
                return
            unlike = u in self._liked_uids
            if unlike:
                self.mark_unliked(u)
            else:
                self.mark_liked(u)
            try:
                self._on_like(u, unlike=unlike)
            except TypeError:
                self._on_like(u)

        def _on_enter(e, u=uid, h=hi, im=img, cw=card_w, ch=thumb_h):
            if im is not None and h:
                try:
                    im.src = h
                    im.update()
                except Exception:
                    pass
            rect = screen_rect_for_card_image(self._page, e, cw, ch, rail_w=_RAIL_W, meta_h=68)
            if u and rect:
                self._rect_by_uid[u] = rect

        def _on_hover(e, u=uid, cw=card_w, ch=thumb_h):
            rect = screen_rect_for_card_image(self._page, e, cw, ch, rail_w=_RAIL_W, meta_h=68)
            if u and rect:
                self._rect_by_uid[u] = rect

        like_btn = ft.IconButton(
            icon=ft.Icons.FAVORITE if liked else ft.Icons.FAVORITE_BORDER,
            icon_size=16,
            icon_color="#f9a8d4" if liked else "#ffffff",
            tooltip="Unlike" if liked else "Like",
            disabled=not uid,
            style=ft.ButtonStyle(padding=0),
            on_click=_like,
        )
        if uid:
            self._like_btns[uid] = like_btn

        disk_btn = ft.IconButton(
            icon=ft.Icons.SAVE_ALT,
            icon_size=16,
            icon_color=ft.Colors.GREEN_300 if on_disk else "#94a3b8",
            tooltip="On disk" if on_disk else "Not on disk",
            disabled=not on_disk,
            style=ft.ButtonStyle(padding=0),
        )
        if uid:
            self._disk_icon_by_uid[uid.lower()] = disk_btn

        rail_kids: list[ft.Control] = [
            ft.IconButton(
                icon=ft.Icons.VIEW_IN_AR,
                icon_size=16,
                icon_color="#67e8f9",
                tooltip="Preview",
                style=ft.ButtonStyle(padding=0),
                on_click=_preview,
            ),
        ]
        dl_color, dl_tip = download_icon_style(dl)
        rail_kids.append(
            ft.IconButton(
                icon=ft.Icons.DOWNLOAD_FOR_OFFLINE,
                icon_size=16,
                icon_color=dl_color,
                tooltip=dl_tip,
                style=ft.ButtonStyle(padding=0),
                on_click=lambda e, u=uid, n=name: self._on_download(u, n) if self._on_download else None,
            )
        )
        rail_kids.extend([disk_btn, like_btn])

        rail = ft.Container(
            left=0,
            top=0,
            bottom=0,
            width=_RAIL_W,
            bgcolor=_RAIL_TINT,
            padding=2,
            content=ft.Column(
                rail_kids,
                spacing=0,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        title = (
            ft.TextButton(
                text=name[: max(18, card_w // 6)],
                url=model_url,
                style=ft.ButtonStyle(padding=0, color="#ffffff"),
                tooltip=name,
            )
            if model_url
            else ft.Text(
                name[: max(18, card_w // 6)],
                size=12,
                weight=ft.FontWeight.W_700,
                color="#ffffff",
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
        )
        author_ctrl = (
            ft.TextButton(
                text=author,
                style=ft.ButtonStyle(padding=0, color=_META_COLOR),
                tooltip=f"Open @{author_key} in Account" if author_key else None,
                on_click=lambda e, k=author_key: self._on_open_account(k) if self._on_open_account and k else None,
            )
            if author and author_key and self._on_open_account
            else ft.Text(author or "", size=10, color=_META_COLOR, max_lines=1)
        )
        text_w = max(_RAIL_W + 8, min(card_w - 8, int(card_w * 0.72)))
        bottom = ft.Container(
            left=0,
            bottom=0,
            width=text_w,
            bgcolor=_RAIL_TINT,
            padding=ft.padding.only(left=_RAIL_W + 4, right=6, top=6, bottom=6),
            content=ft.Column(
                [
                    title,
                    author_ctrl,
                    ft.Text(stats, size=10, weight=ft.FontWeight.W_600, color=_META_COLOR, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ],
                spacing=1,
                tight=True,
            ),
        )
        badges: list[ft.Control] = []
        if dl:
            badges.append(
                ft.Container(
                    right=4,
                    top=4,
                    bgcolor=ft.Colors.with_opacity(0.92, "#166534"),
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=5, vertical=2),
                    content=ft.Text("DL", size=8, weight=ft.FontWeight.BOLD, color="#bbf7d0", no_wrap=True),
                )
            )
        if on_disk:
            badges.append(
                ft.Container(
                    right=4,
                    bottom=4,
                    bgcolor=ft.Colors.with_opacity(0.92, "#0f766e"),
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=5, vertical=2),
                    content=ft.Text("DISK", size=8, weight=ft.FontWeight.BOLD, color="#ccfbf1", no_wrap=True),
                )
            )
        stack = ft.Stack(
            [thumb_box, bottom, rail, *badges],
            height=thumb_h,
        )
        return ft.Container(
            width=card_w,
            height=thumb_h,
            bgcolor="#111827",
            border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.GestureDetector(
                content=stack,
                hover_interval=40,
                on_enter=_on_enter,
                on_hover=_on_hover,
            ),
        )
