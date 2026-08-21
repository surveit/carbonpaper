"""The string one record, stage or run points at another by."""
from __future__ import annotations

from typing import TypeAlias

# An alias, not a NewType: mypy cannot tell ID from str, so the name rule enforces it.
ID: TypeAlias = str
