"""What one column of a stored file holds; see docs/run-and-review-ui.md."""
from __future__ import annotations

import enum
import re
from collections import Counter
from collections.abc import Sequence

from typing import ClassVar

from pydantic import BaseModel

from app.core.column_profile import NumericRange, ValueCount
from app.core.ids import ID
from app.core.record import PersistedModel, PersistenceScope

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")
# How many of a column's commonest values a stored shape keeps, for every reader of it.
VALUES_KEPT = 8

_HISTOGRAM_BINS = 12
# Above this many distinct values a column reads as prose, not a set.
_CATEGORY_SHARE = 0.02
_CATEGORY_FLOOR = 24


class ColumnKind(enum.StrEnum):
    NUMBER = "number"
    DATE = "date"
    TIME = "time"
    CATEGORY = "category"
    TEXT = "text"
    CONSTANT = "constant"
    EMPTY = "empty"


class HistogramBin(BaseModel):
    left: float
    right: float
    count: int


class LengthRange(BaseModel):
    min: int
    max: int
    median: int


class ColumnShape(BaseModel):
    """`blank_count` is the empty strings — the emptiness a null count never sees."""

    column: str
    kind: ColumnKind
    null_count: int
    blank_count: int
    filled_count: int
    distinct_count: int
    top: list[ValueCount]
    numbers: NumericRange | None = None
    histogram: list[HistogramBin] = []
    timeline: list[ValueCount] = []
    lengths: LengthRange | None = None


class FileShape(BaseModel):
    row_count: int
    columns: list[ColumnShape]


class StoredFileShape(PersistedModel):
    """A file's bytes never change, so this is measured once and never recomputed."""

    collection: ClassVar[str] = "file_shape"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    file_id: ID
    shape: FileShape


def measure_column_shape(
    column: str, values: Sequence[str], *, null_count: int, max_values: int,
    may_read_as_number: bool = True,
) -> ColumnShape:
    """`values` is every non-null cell as text, in row order; `null_count` is the rest."""
    if max_values < 1:
        raise ValueError(f"max_values must be at least 1, got {max_values}")
    filled = Counter(value for value in values if value.strip())
    blank_count = len(values) - sum(filled.values())
    rows = len(values) + null_count
    kind = classify_column(filled, rows, may_read_as_number=may_read_as_number)
    return ColumnShape(
        column=column, kind=kind, null_count=null_count, blank_count=blank_count,
        filled_count=sum(filled.values()), distinct_count=len(filled),
        top=_rank_values(filled, max_values),
        numbers=summarize_numbers(filled) if kind == ColumnKind.NUMBER else None,
        histogram=bin_numbers(filled) if kind == ColumnKind.NUMBER else [],
        timeline=count_by_day(filled) if kind == ColumnKind.DATE else [],
        lengths=measure_lengths(filled) if kind == ColumnKind.TEXT else None,
    )


def _rank_values(filled: Counter[str], max_values: int) -> list[ValueCount]:
    """Ties break by value, so equally common values do not reorder between reads."""
    ranked = sorted(filled.items(), key=lambda seen: (-seen[1], seen[0]))
    return [ValueCount(value=value, count=count) for value, count in ranked[:max_values]]


def classify_column(
    filled: Counter[str], rows: int, *, may_read_as_number: bool = True
) -> ColumnKind:
    """What a reader should be told first about this column."""
    if not filled:
        return ColumnKind.EMPTY
    if len(filled) == 1 and sum(filled.values()) == rows:
        return ColumnKind.CONSTANT
    # False where a schema declares `str`: "001902" reads as 1902, a range it has not.
    if may_read_as_number and all(_reads_as_number(value) for value in filled):
        return ColumnKind.NUMBER
    if all(_DATE.match(value) for value in filled):
        return ColumnKind.DATE
    if all(_TIME.match(value) for value in filled):
        return ColumnKind.TIME
    if len(filled) <= max(_CATEGORY_FLOOR, sum(filled.values()) * _CATEGORY_SHARE):
        return ColumnKind.CATEGORY
    return ColumnKind.TEXT


def summarize_numbers(filled: Counter[str]) -> NumericRange:
    numbers = sorted(float(value) for value in filled)
    weights = [filled[value] for value in sorted(filled, key=float)]
    total = sum(weights)
    return NumericRange(
        min=numbers[0], max=numbers[-1],
        mean=sum(n * w for n, w in zip(numbers, weights, strict=True)) / total,
        median=_weighted_median(numbers, weights),
    )


def bin_numbers(filled: Counter[str]) -> list[HistogramBin]:
    numbers = {float(value): count for value, count in filled.items()}
    low, high = min(numbers), max(numbers)
    if low == high:
        return [HistogramBin(left=low, right=high, count=sum(numbers.values()))]
    width = (high - low) / _HISTOGRAM_BINS
    counts = [0] * _HISTOGRAM_BINS
    for number, count in numbers.items():
        counts[min(int((number - low) / width), _HISTOGRAM_BINS - 1)] += count
    return [HistogramBin(left=low + i * width, right=low + (i + 1) * width, count=count)
            for i, count in enumerate(counts)]


def count_by_day(filled: Counter[str]) -> list[ValueCount]:
    return [ValueCount(value=day, count=filled[day]) for day in sorted(filled)]


def measure_lengths(filled: Counter[str]) -> LengthRange:
    lengths = sorted(len(value) for value in filled)
    weights = [filled[value] for value in sorted(filled, key=len)]
    return LengthRange(min=lengths[0], max=lengths[-1],
                       median=int(_weighted_median(lengths, weights)))


def _weighted_median(ordered: Sequence[float], weights: Sequence[int]) -> float:
    half, seen = sum(weights) / 2, 0
    for value, weight in zip(ordered, weights, strict=True):
        seen += weight
        if seen >= half:
            return float(value)
    return float(ordered[-1])


def _reads_as_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True
