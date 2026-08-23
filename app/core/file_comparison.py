"""Which columns tell one file from the others of its shape.

Nothing here decides what a column MEANS; a column earns its place by how much the
files disagree about it, and only ever against files it shares a schema with.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import combinations

from pydantic import BaseModel

from app.core.column_profile import ValueCount
from app.core.file_shape import ColumnShape, FileShape
from app.core.ids import ID

# What share of a file's rows a column's listed values must cover for the column to be
# compared at all. Under it the values are a sample of a long tail — an id, a headline,
# a timestamp — and two files disagreeing about them says nothing.
_COVERAGE_FLOOR = 0.5

# Below this the files agree closely enough that naming the column would be noise.
_DISAGREEMENT_FLOOR = 0.15

_REST = "\0the rest"


class ShapeGroup(BaseModel):
    """The files carrying one set of columns — the only files worth comparing."""

    columns: tuple[str, ...]
    file_ids: list[ID]


class ColumnDisagreement(BaseModel):
    column: str
    # 0 when every file's values are distributed alike, 1 when no two share a value.
    score: float


def group_files_by_columns(shape_by_file_id: Mapping[ID, FileShape]) -> list[ShapeGroup]:
    """Files with the same columns in the same order, largest group first."""
    grouped_file_ids: dict[tuple[str, ...], list[ID]] = {}
    for file_id, shape in shape_by_file_id.items():
        grouped_file_ids.setdefault(
            tuple(c.column for c in shape.columns), []).append(file_id)
    return sorted(
        (ShapeGroup(columns=columns, file_ids=file_ids)
         for columns, file_ids in grouped_file_ids.items()),
        key=lambda group: (-len(group.file_ids), group.file_ids))


def choose_the_telling_column(shapes: Sequence[FileShape]) -> ColumnDisagreement | None:
    """The column whose commonest values differ across most of these files."""
    ranked = rank_columns_by_disagreement(shapes)
    if not ranked:
        return None
    return max(ranked, key=lambda seen: (_count_leading_values(shapes, seen.column),
                                         seen.score))


def _count_leading_values(shapes: Sequence[FileShape], column: str) -> int:
    leading = {found.value for shape in shapes
               if (found := read_leading_value(shape, column)) is not None}
    return len(leading)


def read_leading_value(shape: FileShape, column: str) -> ValueCount | None:
    """The value this file holds most of in one column, or None where it holds none."""
    found = _find_column(shape, column)
    if found is None or not found.top or not found.filled_count:
        return None
    return found.top[0]


def rank_columns_by_disagreement(shapes: Sequence[FileShape]) -> list[ColumnDisagreement]:
    """Most disagreed-about first. Empty for one file, which has nothing to differ from."""
    if len(shapes) < 2:
        return []
    ranked = [ColumnDisagreement(column=column, score=score)
              for column in _compare_these_columns(shapes)
              if (score := _measure_disagreement(shapes, column)) >= _DISAGREEMENT_FLOOR]
    return sorted(ranked, key=lambda seen: (-seen.score, seen.column))


def _compare_these_columns(shapes: Sequence[FileShape]) -> list[str]:
    # A date column is left out: its span says more than any one day's share.
    return [column.column for column in shapes[0].columns
            if not column.timeline
            and all(_read_distribution(shape, column.column) is not None for shape in shapes)]


def _measure_disagreement(shapes: Sequence[FileShape], column: str) -> float:
    """Mean pairwise total-variation distance between the files' distributions."""
    spreads = [_read_distribution(shape, column) for shape in shapes]
    pairs = list(combinations([s for s in spreads if s is not None], 2))
    if not pairs:
        return 0.0
    return sum(_measure_distance(a, b) for a, b in pairs) / len(pairs)


def _measure_distance(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    return sum(abs(a.get(value, 0.0) - b.get(value, 0.0))  # data-default-ok: absent = share zero
               for value in set(a) | set(b)) / 2


def _read_distribution(shape: FileShape, column: str) -> dict[str, float] | None:
    """None when this column is a long tail here, so no two files can disagree usefully."""
    found = _find_column(shape, column)
    if found is None or not found.filled_count:
        return None
    shares = {value.value: value.count / found.filled_count for value in found.top}
    covered = sum(shares.values())
    if covered < _COVERAGE_FLOOR:
        return None
    # What the listed values leave out is one bucket: two files whose tails are
    # different sizes disagree by that much, whatever is in them.
    shares[_REST] = max(0.0, 1 - covered)
    return shares


def _find_column(shape: FileShape, column: str) -> ColumnShape | None:
    return next((c for c in shape.columns if c.column == column), None)
