"""What a diff says about one column and one cell."""

from __future__ import annotations

from enum import Enum


class ColumnDiffState(str, Enum):
    carried = "carried"
    dropped = "dropped"
    added = "added"
    # The three the values-used sheet adds (app.web.values_view).
    rewritten = "rewritten"
    read = "read"
    absent = "absent"


class CellDiffState(str, Enum):
    carried = "carried"
    changed = "changed"
    dropped = "dropped"
    added = "added"
