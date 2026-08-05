"""Profile columns of a stage's stored output: the values one run really produced,
so a declared schema comes from the data rather than from the methodology's prose."""
from __future__ import annotations

import pandas as pd

from app.models.column_profile import ColumnProfile, NumericRange, StageOutputProfile, ValueCount
from app.services.run import read_stage_output


def profile_stage_output(
    project: str, run_id: str, stage_id: str, columns: list[str], *, max_values: int,
) -> StageOutputProfile:
    """Every miss raises, naming what exists — never an empty or partial profile."""
    if max_values < 1:
        raise ValueError(f"max_values must be at least 1, got {max_values}")
    frame = read_stage_output(project, run_id, stage_id)
    return StageOutputProfile(
        run_id=run_id,
        stage_id=stage_id,
        row_count=len(frame),
        columns=[_profile_column(frame, name, max_values) for name in columns],
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
        value_range=_summarize_numeric_range(present),
    )


def _count_distinct_values(present: pd.Series) -> list[ValueCount]:
    """Commonest first, in text form — which keeps a list/dict cell countable."""
    as_text = present.map(lambda value: value if isinstance(value, str) else str(value))
    counted = [
        ValueCount(value=str(value), count=int(count))
        for value, count in as_text.value_counts().items()
    ]
    return sorted(counted, key=lambda seen: (-seen.count, seen.value))


def _summarize_numeric_range(present: pd.Series) -> NumericRange | None:
    """None for anything not numerically typed — including a numeric-looking `str` column."""
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
