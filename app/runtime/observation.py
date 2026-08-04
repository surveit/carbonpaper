"""Observed distinct-value profiles of input_data stages. Loads the stage's bound
file through the SAME read path a run's input_data handler uses (load_input_frame:
dtype pinning, date parsing and all), then reports per-column value profiles. The
composition roots (app.web, app.mcp) inject `profile_input_stage` into
app.services.observation, so the services layer stays runtime-free."""
from __future__ import annotations

import pandas as pd

from app.models import Stage, StageType
from app.models.observation import (
    DEFAULT_MAX_DISTINCT_VALUES,
    ColumnValueProfile,
    InputFrameProfile,
)
from app.runtime.stages.input_data import load_input_frame


def profile_input_stage(
    stage: Stage, max_values: int = DEFAULT_MAX_DISTINCT_VALUES
) -> InputFrameProfile:
    """Load an input_data stage's bound file and profile it. Fails loudly on any miss."""
    # No fabrication anywhere on this path: an unbound path or a missing file
    # raises out of load_input_frame / pandas, never an empty profile.
    if stage.type != StageType.input_data:
        raise ValueError(
            f"stage '{stage.id}' is `{stage.type}`, not `input_data` — only an "
            "input stage's file can be observed"
        )
    return profile_frame(load_input_frame(stage), max_values)


def profile_frame(
    frame: pd.DataFrame, max_values: int = DEFAULT_MAX_DISTINCT_VALUES
) -> InputFrameProfile:
    """Profile every column, listing at most `max_values` distinct values per column."""
    if max_values < 1:
        raise ValueError(f"max_values must be at least 1, got {max_values}")
    return InputFrameProfile(
        row_count=len(frame),
        columns=[
            _profile_column(str(name), frame[name], max_values) for name in frame.columns
        ],
    )


def _profile_column(name: str, series: pd.Series, max_values: int) -> ColumnValueProfile:
    non_null = series.dropna()
    # String form first: it is what the profile reports, and it keeps unhashable
    # cells (a parsed list/json column) countable without special-casing them.
    distinct = sorted({_cell_text(value) for value in non_null})
    # distinct_count is the TRUE count even when `values` is truncated — that gap
    # is what tells a reader the list is not the whole vocabulary.
    return ColumnValueProfile(
        name=name,
        row_count=len(series),
        null_count=len(series) - len(non_null),
        distinct_count=len(distinct),
        values=distinct[:max_values],
    )


def _cell_text(value: object) -> str:
    # A str cell passes through untouched; everything else reports its str() form
    # (so 2 and "2" collapse — the profile is about vocabulary, not dtype).
    return value if isinstance(value, str) else str(value)
