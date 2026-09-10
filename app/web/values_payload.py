"""What the Relevant columns tab is handed: the run's graph, read as one walk."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.branch_analysis import BranchId, BranchOption, RowOrdinal
from app.models.schema import StageId


class MinimapNode(BaseModel):
    stage_id: StageId
    glyph: str
    # Whether this stage wrote or carried a column the cited value came through.
    on_walk: bool
    rows_behind: int


class MinimapEdge(BaseModel):
    from_stage: StageId
    to_stage: StageId
    # None where the parent is off the walk, so no row count speaks for the wire.
    rows: int | None


class MinimapCut(BaseModel):
    """Rows a stage took out of the workflow, offered as a page of their own."""

    stage_id: StageId
    branch: BranchId
    label: str
    tip: str
    # The branch's recorded count over the whole run.
    rows: int


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
    description: str
    inputs: list[StageId]
    rows_in: int
    rows_out: int
    rows_dropped: int
    rows_behind: int
    columns: list[str]
    rows: list[SheetRow]


class MinimapArm(BaseModel):
    """One way a stage told this figure's rows apart: a set of branches and their count."""

    stage_id: StageId
    branches: list[BranchId]
    label: str
    tip: str
    rows: int


class StepSource(BaseModel):
    stage_id: StageId
    rows: int


class ValuesUsed(BaseModel):
    cited_stage: StageId
    column: str
    row: int
    # The stages the value came through, upstream first; each panel is fetched.
    steps: list[StageId]
    # The run's own workflow graph, drawn by the same builder every other page uses.
    mermaid: str
    nodes: list[MinimapNode]
    edges: list[MinimapEdge]
    sources: dict[StageId, list[StepSource]]
    cuts: list[MinimapCut]
    # Only stages that split this figure's rows more than one way are listed.
    arms: dict[StageId, list[MinimapArm]]
    # What an arm's chip lights in the code: the branch's lines, and the code itself.
    branches: dict[BranchId, BranchOption]
    code: dict[StageId, str]
    # Set where the cited column is a `count`, which reads no column.
    counts_rows: bool
    # In the run's stage order, one per stage that wrote a frame.
    sheets: list[CanvasSheet]
