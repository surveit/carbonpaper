"""What a frame holds, per column: the shape a schema gets declared from."""
from __future__ import annotations

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
