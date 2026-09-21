"""What the Rows & columns tab is handed: the run's graph, read as one walk."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.branch_analysis import RowOrdinal
from app.models.schema import StageId


class CanvasNode(BaseModel):
    stage_id: StageId
    glyph: str
    # Whether this stage wrote or carried a column the cited value came through.
    on_walk: bool
    rows_behind: int


class CanvasEdge(BaseModel):
    from_stage: StageId
    to_stage: StageId
    # None where the parent is off the walk, so no row count speaks for the wire.
    rows: int | None


class SheetRow(BaseModel):
    # None for a dropped row, which has no ordinal in the stage's output frame.
    ordinal: RowOrdinal | None
    # Positional against the sheet's `columns`.
    cells: list[str]
    # Behind the cited figure.
    mine: bool
    dropped: bool


class CanvasSheet(BaseModel):
    """One stage's box on the canvas, and the few rows drawn under it."""

    stage_id: StageId
    type: str
    rows_in: int
    rows_out: int
    rows_dropped: int
    rows_behind: int
    columns: list[str]
    # Those of `columns`, in its order, the cited value came through here.
    columns_behind: list[str]
    rows: list[SheetRow]


class CanvasView(BaseModel):
    cited_stage: StageId
    column: str
    row: int
    # The stages the value came through, upstream first; each panel is fetched.
    steps: list[StageId]
    nodes: list[CanvasNode]
    edges: list[CanvasEdge]
    # Set where the cited column is a `count`, which reads no column.
    counts_rows: bool
    # In the run's stage order, one per stage that wrote a frame.
    sheets: list[CanvasSheet]
