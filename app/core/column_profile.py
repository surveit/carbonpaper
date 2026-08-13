"""What a frame holds, per column: the shape a schema gets declared from, and the
measurement that fills it."""
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel


class ValueCount(BaseModel):
    value: str
    count: int


class NumericRange(BaseModel):
    min: float
    max: float
    mean: float
    median: float


class ColumnProfile(BaseModel):
    """`distinct_count` is the TRUE count of distinct non-null values, even when cut."""

    column: str
    null_count: int
    distinct_count: int
    values: list[ValueCount]
    truncated: bool
    value_range: NumericRange | None = None


class StageOutputProfile(BaseModel):
    run_id: str
    stage_id: str
    row_count: int
    columns: list[ColumnProfile]


class TableProfile(BaseModel):
    row_count: int
    columns: list[ColumnProfile]


def profile_frame(
    frame: pd.DataFrame, columns: list[str] | None, *, max_values: int
) -> TableProfile:
    """Every miss raises, naming what exists — never an empty or partial profile."""
    if max_values < 1:
        raise ValueError(f"max_values must be at least 1, got {max_values}")
    return TableProfile(
        row_count=len(frame),
        columns=[_profile_column(frame, name, max_values)
                 for name in (columns if columns is not None else list(frame.columns))],
    )


def _profile_column(frame: pd.DataFrame, name: str, max_values: int) -> ColumnProfile:
    if name not in frame.columns:
        raise ValueError(
            f"the output holds no column '{name}' — its columns: "
            + (", ".join(str(column) for column in frame.columns) or "(none)")
        )
    present = frame[name].dropna()
    counts = _count_distinct_values(present)
    return ColumnProfile(
        column=name,
        null_count=len(frame) - len(present),
        distinct_count=len(counts),
        values=counts[:max_values],
        truncated=len(counts) > max_values,
        value_range=_summarize_numeric_range(_read_text_as_numbers(present)),
    )


def _read_text_as_numbers(present: pd.Series) -> pd.Series:
    """All or nothing: one value that is not a number means the column is not one."""
    if present.empty or pd.api.types.is_numeric_dtype(present):
        return present
    converted = pd.to_numeric(present, errors="coerce")
    return present if converted.isna().any() else converted


def _count_distinct_values(present: pd.Series) -> list[ValueCount]:
    """Commonest first, in text form — which keeps a list/dict cell countable."""
    as_text = present.map(lambda value: value if isinstance(value, str) else str(value))
    counted = [
        ValueCount(value=str(value), count=int(count))
        for value, count in as_text.value_counts().items()
    ]
    return sorted(counted, key=lambda seen: (-seen.count, seen.value))


def _summarize_numeric_range(present: pd.Series) -> NumericRange | None:
    if present.empty or not pd.api.types.is_numeric_dtype(present):
        return None
    if pd.api.types.is_bool_dtype(present):
        return None
    return NumericRange(
        min=float(present.min()),
        max=float(present.max()),
        mean=float(present.mean()),
        median=float(present.median()),
    )
