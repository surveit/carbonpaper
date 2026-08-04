"""Observed value profiles of a frame a run actually produced: what the data
holds, per column, so an authoring agent can decide which observed vocabularies
to freeze as a declared `enum`. Pure shapes only — app.services.observation does
the profiling and serves one column by name."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# How many distinct values a profile returns when the caller names no maximum —
# a 100k-value id column must not flood a reader who only needs to see "this is
# not categorical". A caller who expects a large closed vocabulary (commodity
# codes, country codes) asks for more.
DEFAULT_MAX_DISTINCT_VALUES = 40


class ColumnValueProfile(BaseModel):
    """One column's observed values: counts always, values up to the caller's maximum."""

    model_config = ConfigDict(extra="forbid")

    name: str
    row_count: int = Field(description="Rows in the frame (including null cells of this column).")
    null_count: int = Field(description="Cells of this column that are null/missing.")
    distinct_count: int = Field(
        description=(
            "The TRUE count of distinct non-null values observed — the whole column, "
            "never capped by the maximum `values` was truncated to."
        )
    )
    values: list[str] = Field(
        description=(
            "The observed distinct values, sorted, in string form, truncated to the "
            "maximum the caller asked for. COMPLETE only when distinct_count == "
            "len(values); distinct_count > len(values) means this list is a truncated "
            "prefix and NOT the column's vocabulary — re-read with a larger maximum "
            "before treating it as one."
        )
    )


class ObservedColumnValues(ColumnValueProfile):
    """One column's profile plus the run and stage output it was read from."""

    run_id: str = Field(
        description=(
            "The run whose stored output was read. The profile describes THAT run's "
            "rows and nothing wider — a different slice of the source, or a rerun "
            "after an upstream edit, can hold values this one never saw."
        )
    )
    stage_id: str = Field(
        description=(
            "The stage whose output was read. `row_count` is that output's size, "
            "which downstream of a filter or an aggregate is far smaller than the "
            "source: a vocabulary frozen off a short tail is a guess, not an "
            "observation."
        )
    )


class FrameProfile(BaseModel):
    """A whole frame's observed profile: one ColumnValueProfile per column."""

    model_config = ConfigDict(extra="forbid")

    row_count: int
    columns: list[ColumnValueProfile]

    def column_named(self, name: str) -> ColumnValueProfile | None:
        """The profile of the column called `name`, or None if the frame has no such column."""
        for column in self.columns:
            if column.name == name:
                return column
        return None
