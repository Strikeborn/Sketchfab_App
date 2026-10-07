"""Flet WebView is macOS/iOS/Android only — Windows/Linux use Chrome/Edge --app."""
from __future__ import annotations

import sys


def webview_supported() -> bool:
    return sys.platform in ("darwin", "android", "ios")
