"""Observed distinct values for the authoring surfaces: profile one column of one
stage's output in one run, so a declared `enum` comes from data a run produced
rather than from the methodology's prose. Any stage is observable — the run's
manifest says where each one's frame landed."""
from __future__ import annotations

import pandas as pd

from app.core.frames import read_frame_file
from app.models.observation import (
    DEFAULT_MAX_DISTINCT_VALUES,
    ColumnValueProfile,
    FrameProfile,
    ObservedColumnValues,
)
from app.services import run as run_service
from app.services import workspace


def observed_column_values(
    project_id: str,
    run_id: str,
    stage_id: str,
    column: str,
    max_values: int = DEFAULT_MAX_DISTINCT_VALUES,
) -> ObservedColumnValues:
    """One column's observed values in a run's stored stage output. Every miss raises."""
    if not workspace.resolve_project_dir(project_id).is_dir():
        raise ValueError(f"no project '{project_id}' in the workspace")
    path = run_service.resolve_stage_output_path(project_id, run_id, stage_id)
    profile = profile_frame(read_frame_file(path), max_values)
    column_profile = profile.column_named(column)
    if column_profile is None:
        observed = ", ".join(c.name for c in profile.columns) or "(none)"
        raise ValueError(
            f"stage '{stage_id}' of run '{run_id}' in project '{project_id}' output "
            f"no column '{column}' — the columns it did output: {observed}"
        )
    return ObservedColumnValues(
        run_id=run_id, stage_id=stage_id, **column_profile.model_dump()
    )


def profile_frame(
    frame: pd.DataFrame, max_values: int = DEFAULT_MAX_DISTINCT_VALUES
) -> FrameProfile:
    """Profile every column, listing at most `max_values` distinct values per column."""
    if max_values < 1:
        raise ValueError(f"max_values must be at least 1, got {max_values}")
    return FrameProfile(
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
