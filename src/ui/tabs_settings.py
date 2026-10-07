"""Settings tab — background sync, defaults, tray."""
from __future__ import annotations

import flet as ft

from app_settings import DEFAULTS, load_settings, save_settings

_PANEL = "#0f172a"


class SettingsTab(ft.Column):
    def __init__(self):
        super().__init__(expand=True, spacing=12, scroll=ft.ScrollMode.AUTO)
        self._settings = load_settings()
        self._status = ft.Text("", size=12, color=ft.Colors.GREY_400)
        self._dry_run = ft.Switch(
            label="Dry-run default (Push simulates only)",
            value=bool(self._settings.get("dry_run_default", False)),
        )

        self._bg_sync = ft.Switch(
            label="Background sync while app runs",
            value=bool(self._settings.get("background_sync_enabled", True)),
        )
        self._interval = ft.TextField(
            label="Background check interval (seconds)",
            value=str(int(self._settings.get("background_sync_interval_sec", 60))),
            width=220,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self._minimize_tray = ft.Switch(
            label="Tray icon enabled (gray × hides; — minimizes to taskbar normally)",
            value=bool(self._settings.get("minimize_to_tray", True)),
        )
        self._close_tray = ft.Switch(
            label="× Close hides to tray (off = normal red × quits the app)",
            value=bool(self._settings.get("close_to_tray", False)),
        )
        self._hide_console = ft.Switch(
            label="Hide CMD window when hidden to tray (Show restores both)",
            value=bool(self._settings.get("hide_console_on_tray", True)),
        )
        self._dual_close = ft.Switch(
            label="Dual close — gray × tray + red × quit in title strip (restart to apply)",
            value=bool(
                self._settings.get(
                    "dual_close_buttons",
                    self._settings.get("custom_window_caption", True),
                )
            ),
        )
        self._custom_caption = self._dual_close  # legacy ref
        self._auto_quick = ft.Switch(
            label="Auto Quick Collect when drift detected (background)",
            value=bool(self._settings.get("auto_quick_collect_on_drift", False)),
        )
        self._notify = ft.Switch(
            label="Activity log when drift detected",
            value=bool(self._settings.get("notify_on_sync_drift", True)),
        )
        self._like_pages = ft.TextField(
            label="Quick Collect max like pages (×100 models)",
            value=str(int(self._settings.get("quick_collect_max_like_pages", 20))),
            width=280,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self._on_save_cb = None
        self._on_exit_cb = None
        self._exit_btn = ft.OutlinedButton(
            "Exit application",
            icon=ft.Icons.POWER_SETTINGS_NEW,
            on_click=lambda e: self._exit(),
        )
        self.controls = [
            ft.Text("Settings", size=20, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Background sync compares /me counts vs workbook, probes newest likes, "
                "and flags collection modelCount drift without listing every model each time.",
                size=12,
                color=ft.Colors.GREY_400,
            ),
            ft.Container(
                bgcolor=_PANEL,
                border_radius=8,
                padding=16,
                content=ft.Column(
                    [
                        self._dry_run,
                        self._bg_sync,
                        self._interval,
                        self._minimize_tray,
                        self._close_tray,
                        self._hide_console,
                        self._dual_close,
                        self._notify,
                        self._auto_quick,
                        self._like_pages,
                        ft.Row(
                            [
                                ft.ElevatedButton(
                                    "Save settings",
                                    icon=ft.Icons.SAVE,
                                    on_click=lambda e: self._save(),
                                ),
                                ft.OutlinedButton(
                                    "Reset defaults",
                                    on_click=lambda e: self._reset(),
                                ),
                                self._exit_btn,
                            ],
                            spacing=8,
                        ),
                        self._status,
                    ],
                    spacing=12,
                ),
            ),
            ft.Text(
                "Dual close adds a thin title strip: — □ then gray × (tray) and red × (quit). "
                "Off = normal Windows title bar with one ×.",
                size=11,
                color=ft.Colors.GREY_500,
            ),
        ]

    def set_on_save(self, cb) -> None:
        self._on_save_cb = cb

    def set_on_exit(self, cb) -> None:
        self._on_exit_cb = cb

    def _exit(self) -> None:
        if self._on_exit_cb:
            self._on_exit_cb()

    def current_settings(self) -> dict:
        return dict(self._settings)

    def _parse_int(self, field: ft.TextField, default: int, *, min_v: int = 1) -> int:
        try:
            return max(min_v, int(str(field.value or default).strip()))
        except (TypeError, ValueError):
            return default

    def _save(self) -> None:
        self._settings = {
            "dry_run_default": bool(self._dry_run.value),
            "background_sync_enabled": bool(self._bg_sync.value),
            "background_sync_interval_sec": self._parse_int(
                self._interval, int(DEFAULTS["background_sync_interval_sec"]), min_v=15
            ),
            "minimize_to_tray": bool(self._minimize_tray.value),
            "close_to_tray": bool(self._close_tray.value),
            "hide_console_on_tray": bool(self._hide_console.value),
            "dual_close_buttons": bool(self._dual_close.value),
            "custom_window_caption": bool(self._dual_close.value),
            "auto_quick_collect_on_drift": bool(self._auto_quick.value),
            "notify_on_sync_drift": bool(self._notify.value),
            "quick_collect_max_like_pages": self._parse_int(
                self._like_pages, int(DEFAULTS["quick_collect_max_like_pages"]), min_v=1
            ),
        }
        save_settings(self._settings)
        self._status.value = "Saved — background sync picks up changes within one interval."
        self._status.update()
        if self._on_save_cb:
            self._on_save_cb(self._settings)

    def _reset(self) -> None:
        self._settings = dict(DEFAULTS)
        save_settings(self._settings)
        self._dry_run.value = bool(DEFAULTS["dry_run_default"])
        self._bg_sync.value = bool(DEFAULTS["background_sync_enabled"])
        self._interval.value = str(DEFAULTS["background_sync_interval_sec"])
        self._minimize_tray.value = bool(DEFAULTS["minimize_to_tray"])
        self._close_tray.value = bool(DEFAULTS["close_to_tray"])
        self._hide_console.value = bool(DEFAULTS["hide_console_on_tray"])
        self._dual_close.value = bool(DEFAULTS["dual_close_buttons"])
        self._auto_quick.value = bool(DEFAULTS["auto_quick_collect_on_drift"])
        self._notify.value = bool(DEFAULTS["notify_on_sync_drift"])
        self._like_pages.value = str(DEFAULTS["quick_collect_max_like_pages"])
        self._status.value = "Reset to defaults."
        self.update()
        if self._on_save_cb:
            self._on_save_cb(self._settings)
