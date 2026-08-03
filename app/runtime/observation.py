"""Observed distinct-value profiles of input_data stages. Loads the stage's bound
file through the SAME read path a run's input_data handler uses (load_input_frame:
dtype pinning, date parsing and all), then reports per-column value profiles. The
composition roots (app.web, app.mcp) inject `profile_input_stage` into
app.services.observation, so the services layer stays runtime-free."""
from __future__ import annotations

import pandas as pd

from app.models import Stage, StageType
from app.models.observation import (
    DISTINCT_FULL_SET_CAP,
    OVER_CAP_SAMPLE_SIZE,
    ColumnValueProfile,
    InputFrameProfile,
)
from app.runtime.stages.input_data import load_input_frame


def profile_input_stage(stage: Stage) -> InputFrameProfile:
    """Load an input_data stage's bound file and profile it. Fails loudly on any miss."""
    # No fabrication anywhere on this path: an unbound path or a missing file
    # raises out of load_input_frame / pandas, never an empty profile.
    if stage.type != StageType.input_data:
        raise ValueError(
            f"stage '{stage.id}' is `{stage.type}`, not `input_data` — only an "
            "input stage's file can be observed"
        )
    return profile_frame(load_input_frame(stage))


def profile_frame(frame: pd.DataFrame) -> InputFrameProfile:
    """Profile every column of an already-loaded frame."""
    return InputFrameProfile(
        row_count=len(frame),
        columns=[_profile_column(str(name), frame[name]) for name in frame.columns],
    )


def _profile_column(name: str, series: pd.Series) -> ColumnValueProfile:
    non_null = series.dropna()
    # String form first: it is what the profile reports, and it keeps unhashable
    # cells (a parsed list/json column) countable without special-casing them.
    distinct = sorted({_cell_text(value) for value in non_null})
    under_cap = len(distinct) <= DISTINCT_FULL_SET_CAP
    return ColumnValueProfile(
        name=name,
        row_count=len(series),
        null_count=len(series) - len(non_null),
        distinct_count=len(distinct),
        values=distinct if under_cap else None,
        sample=None if under_cap else distinct[:OVER_CAP_SAMPLE_SIZE],
    )


def _cell_text(value: object) -> str:
    # A str cell passes through untouched; everything else reports its str() form
    # (so 2 and "2" collapse — the profile is about vocabulary, not dtype).
    return value if isinstance(value, str) else str(value)
