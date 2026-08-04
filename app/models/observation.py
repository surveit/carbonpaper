"""Observed value profiles of a loaded input frame: what the data actually holds,
per column, so an authoring agent can decide which observed vocabularies to freeze
as a declared `enum`. Pure shapes only — the profiling itself lives in
app.runtime.observation, and app.services.observation serves it by name."""
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


class InputFrameProfile(BaseModel):
    """A whole loaded frame's observed profile: one ColumnValueProfile per column."""

    model_config = ConfigDict(extra="forbid")

    row_count: int
    columns: list[ColumnValueProfile]

    def column_named(self, name: str) -> ColumnValueProfile | None:
        """The profile of the column called `name`, or None if the frame has no such column."""
        for column in self.columns:
            if column.name == name:
                return column
        return None
