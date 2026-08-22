"""What a diff says about one column and one cell. Its own module so a reader of
the states does not import the differ that produces them.
"""

from __future__ import annotations

from enum import Enum


class ColumnDiffState(str, Enum):
    carried = "carried"
    dropped = "dropped"
    added = "added"


class CellDiffState(str, Enum):
    carried = "carried"
    changed = "changed"
    dropped = "dropped"
    added = "added"
