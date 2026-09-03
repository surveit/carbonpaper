"""What the Relevant columns tab is handed: the run's graph, read as one walk."""

from __future__ import annotations

from pydantic import BaseModel

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
    # Set where the cited column is a `count`, which reads no column.
    counts_rows: bool
