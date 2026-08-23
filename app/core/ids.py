"""The string one record, stage or run points at another by."""
from __future__ import annotations

from typing import TypeAlias

# An alias, not a NewType: mypy cannot tell ID from str, so the name rule enforces it.
ID: TypeAlias = str


def validate_id(id: ID) -> ID:
    """``:`` is tested directly, not via ``Path.is_absolute()``, which lets ``C:/x`` pass on Linux."""
    if not id or id != id.strip():
        raise ValueError(f"empty or untrimmed id: {id!r}")
    if id.startswith("/") or "\\" in id or "\x00" in id or ":" in id:
        raise ValueError(f"unsafe id: {id!r}")
    if any(part in ("", "..") for part in id.split("/")):
        raise ValueError(f"unsafe id (empty or '..' segment): {id!r}")
    return id
