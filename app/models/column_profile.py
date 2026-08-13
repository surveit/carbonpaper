"""What a stage output or a stored source file actually holds, per column: the shape
a schema gets declared from."""
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


class StoredFileProfile(BaseModel):
    sha256: str
    filename: str
    row_count: int
    columns: list[ColumnProfile]


class SheetView(BaseModel):
    """`top_left` is the sheet's own first cells, before any header row is picked."""

    sheet_name: str
    row_count: int
    column_count: int
    top_left: list[list[str | None]]


class WorkbookSurvey(BaseModel):
    """What an xlsx holds before a sheet is picked; profile again naming one."""

    sha256: str
    filename: str
    sheets: list[SheetView]
