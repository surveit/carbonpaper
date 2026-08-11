"""The cross-row rules a dataframe must satisfy at a stage boundary: primary-key
uniqueness and exact duplicate rows. Keyed on plain column names rather than a
TableSchema, so the domain layer and the runtime apply the same checks to the
same frame — see app.runtime.validation for the per-column, per-cell half.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pandas as pd

# Duplicate-row groups named individually before the rest are counted off.
_NAMED_GROUP_LIMIT = 5


@dataclass(frozen=True)
class FrameViolation:
    """One broken cross-row rule; `columns` is None for a whole-row rule."""
    columns: list[str] | None
    message: str


def find_frame_violations(df: pd.DataFrame) -> list[FrameViolation]:
    return find_duplicate_row_violations(df)


def find_duplicate_row_violations(df: pd.DataFrame) -> list[FrameViolation]:
    groups = _find_duplicate_row_groups(df)
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


def _find_duplicate_row_groups(df: pd.DataFrame) -> list[list[int]]:
    if df is None or len(df) == 0:
        return []
    groups: dict[str, list[int]] = {}
    for pos, cells in enumerate(df.itertuples(index=False, name=None)):
        # repr() not str(), so cells of different types with the same face value
        # ("1" vs 1) stay distinct, and NaN/None/lists all render.
        rendered = "\x1f".join(repr(c) for c in cells)
        digest = hashlib.sha1(rendered.encode("utf-8")).hexdigest()
        groups.setdefault(digest, []).append(pos)
    return [positions for positions in groups.values() if len(positions) > 1]
