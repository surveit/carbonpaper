"""Reads one JSON document off disk and enforces the one thing every caller
needs before it can go further: the bytes parse as JSON and the JSON is an
object. Domain models parse the returned dict further; this module does not."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.errors import InvalidJsonDocument


def read_json_document(path: Path) -> dict[str, Any]:
    """Raises FileNotFoundError when `path` is absent, InvalidJsonDocument when its bytes are not JSON."""
    if not path.exists():
        raise FileNotFoundError(f"No document at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidJsonDocument(f"{path} does not parse as JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidJsonDocument(f"{path} holds a JSON {type(raw).__name__}, not an object")
    return raw
