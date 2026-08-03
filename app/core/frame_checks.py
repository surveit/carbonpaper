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


def find_frame_violations(
    df: pd.DataFrame, *, primary_key: list[str] | None
) -> list[FrameViolation]:
    """Every cross-row rule the runner enforces on a stage input."""
    return find_primary_key_violations(df, primary_key) + find_duplicate_row_violations(df)


def find_primary_key_violations(
    df: pd.DataFrame, primary_key: list[str] | None
) -> list[FrameViolation]:
    """Rows sharing a primary key. No key declared is nothing to check."""
    # An absent key column is the per-column check's finding ("Missing column"),
    # so it is passed over here rather than reported twice in different words.
    if not (primary_key and all(c in df.columns for c in primary_key)):
        return []
    duplicated = df.duplicated(subset=primary_key).sum()
    if not duplicated:
        return []
    return [
        FrameViolation(
            list(primary_key), f"Primary key duplicated on {duplicated} row(s)"
        )
    ]


def find_duplicate_row_violations(df: pd.DataFrame) -> list[FrameViolation]:
    """Groups of rows identical across every column."""
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
    """Groups of 0-based row positions whose FULL row content is identical."""
    # Identity is a content hash over every column's rendered value; the declared
    # primary_key plays no part in it (that key is optional, and a frame may
    # legitimately repeat one). repr() rather than str() so cells of different
    # types with the same face value ("1" vs 1) stay distinct, and NaN/None/lists
    # all render.
    if df is None or len(df) == 0:
        return []
    groups: dict[str, list[int]] = {}
    for pos, cells in enumerate(df.itertuples(index=False, name=None)):
        rendered = "\x1f".join(repr(c) for c in cells)
        digest = hashlib.sha1(rendered.encode("utf-8")).hexdigest()
        groups.setdefault(digest, []).append(pos)
    return [positions for positions in groups.values() if len(positions) > 1]
