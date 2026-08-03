"""Observed value profiles of a loaded input frame: what the data actually holds,
per column, so an authoring agent can decide which observed vocabularies to freeze
as a declared `enum`. Pure shapes only — the profiling itself lives in
app.runtime.observation, and app.services.observation serves it by name."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A column whose distinct count fits under this cap reports its COMPLETE observed
# value set; over it, only the count plus a small sample — a 100k-value id column
# must not flood the reader who only needs to see "this is not categorical".
DISTINCT_FULL_SET_CAP = 40

# How many values an over-cap column still shows (the first of the sorted set),
# so the reader sees what KIND of values the column holds, never mistaking the
# sample for the whole vocabulary.
OVER_CAP_SAMPLE_SIZE = 10


class ColumnValueProfile(BaseModel):
    """One column's observed values: counts always, the full distinct set only under the cap."""

    model_config = ConfigDict(extra="forbid")

    name: str
    row_count: int = Field(description="Rows in the frame (including null cells of this column).")
    null_count: int = Field(description="Cells of this column that are null/missing.")
    distinct_count: int = Field(description="Count of distinct non-null values observed.")
    values: list[str] | None = Field(
        default=None,
        description=(
            "The COMPLETE observed distinct set (sorted, in string form) when "
            f"distinct_count <= {DISTINCT_FULL_SET_CAP}; None when over the cap — "
            "see `sample` instead."
        ),
    )
    sample: list[str] | None = Field(
        default=None,
        description=(
            f"The first {OVER_CAP_SAMPLE_SIZE} of the sorted distinct set when the "
            "column is over the cap — a taste of the values, NOT the whole set. "
            "None when `values` carries the complete set."
        ),
    )

    @model_validator(mode="after")
    def _full_set_or_sample(self) -> "ColumnValueProfile":
        # Exactly one of the two is present, so a reader can never mistake a
        # sample for the complete vocabulary (or vice versa).
        if (self.values is None) == (self.sample is None):
            raise ValueError(
                f"column {self.name!r}: exactly one of `values` (complete set) or "
                "`sample` (over-cap taste) must be set"
            )
        return self


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
