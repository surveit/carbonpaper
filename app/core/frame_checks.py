"""The cross-row rules rows must satisfy at a stage boundary: primary-key
uniqueness and exact duplicate rows. Takes ROWS, not a frame or a table: row
identity needs neither type system, and one of the two callers holds authored
test rows that no column type has been agreed for yet.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


# Duplicate-row groups named individually before the rest are counted off.
_NAMED_GROUP_LIMIT = 5


@dataclass(frozen=True)
class FrameViolation:
    """One broken cross-row rule; `columns` is None for a whole-row rule."""
    columns: list[str] | None
    message: str


Row = Mapping[str, Any]


def find_frame_violations(rows: Sequence[Row]) -> list[FrameViolation]:
    """Every cross-row rule the runner enforces on a stage input."""
    return find_duplicate_row_violations(rows)


def find_duplicate_row_violations(rows: Sequence[Row]) -> list[FrameViolation]:
    """Groups of rows identical across every column."""
    groups = _find_duplicate_row_groups(rows)
    if not groups:
        return []
    shown = "; ".join(f"rows {group}" for group in groups[:_NAMED_GROUP_LIMIT])
    unshown = len(groups) - _NAMED_GROUP_LIMIT
    more = f" (+{unshown} more group(s))" if unshown > 0 else ""
    return [
        FrameViolation(
            None,
            f"exact duplicate rows: {shown}{more} (0-based row numbers). Duplicates "
            "at a stage boundary are ambiguous intent — an upstream bug, or sampling "
            "smuggled in implicitly. If N draws per row are intended, add an explicit "
            "row_id/draw_id column upstream so the rows are distinct.",
        )
    ]


def _find_duplicate_row_groups(rows: Sequence[Row]) -> list[list[int]]:
    """Groups of 0-based row positions whose FULL row content is identical."""
    # Identity is a content hash over each row's keys AND rendered values, sorted
    # so key order cannot change it. repr() rather than str() so cells of
    # different types with the same face value ("1" vs 1) stay distinct, and
    # None/lists all render. The digest never leaves this function — it groups
    # rows within one call and is not the stage cache's fingerprint, so what it
    # renders is free to change.
    if not rows:
        return []
    groups: dict[str, list[int]] = {}
    for pos, row in enumerate(rows):
        rendered = "\x1f".join(f"{k}={row[k]!r}" for k in sorted(row))
        digest = hashlib.sha1(rendered.encode("utf-8")).hexdigest()
        groups.setdefault(digest, []).append(pos)
    return [positions for positions in groups.values() if len(positions) > 1]
