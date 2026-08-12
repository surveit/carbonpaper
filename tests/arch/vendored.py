"""The third-party files in app/static, and the upstream release each must equal.

Named here is what the colour and vocabulary rules skip, on the grounds that upstream
wrote it. Anything not named is authored and gets no exemption.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"

HIGHLIGHT_JS_VERSION = "11.11.1"

# The SRI cdnjs publishes for the release, not a hash computed from what was
# downloaded: https://api.cdnjs.com/libraries/highlight.js/11.11.1?fields=sri
VENDORED_SRI = {
    "highlight.min.js": (
        "sha512-EBLzUL8XLl+va/zAsmXwS7Z2B1F9HUHkZwyS/VKwh3S7T/U0nF4BaU29EP/ZSf6zgiIx"
        "YAnKLu6bJ8dqpmX5uw=="
    ),
    "hljs-github-dark.css": (
        "sha512-rO+olRTkcf304DQBxSWxln8JXCzTHlKnIdnMUwYvQa9/Jd4cQaNkItIUj6Z4nvW1dqK0"
        "SKXLbn9h4KwZTNtAyw=="
    ),
}


def is_vendored(path: Path) -> bool:
    return path.parent == STATIC and path.name in VENDORED_SRI


def read_sri(path: Path) -> str:
    if not path.is_file():
        return f"missing: {path.name}"
    return "sha512-" + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()
