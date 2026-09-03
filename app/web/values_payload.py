"""What the Relevant columns tab is handed, one entry per stage that wrote."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.schema import StageId


class MinimapNode(BaseModel):
    stage_id: StageId
    glyph: str


class MinimapEdge(BaseModel):
    from_stage: StageId
    to_stage: StageId
    columns: int


class StepSource(BaseModel):
    stage_id: StageId
    columns: list[str]


class ValuesUsed(BaseModel):
    cited_stage: StageId
    column: str
    row: int
    # The stages the value came through, upstream first; each panel is fetched.
    steps: list[StageId]
    # Left to right, each entry one column of the minimap.
    minimap: list[list[MinimapNode]]
    edges: list[MinimapEdge]
    sources: dict[StageId, list[StepSource]]
    # Set where the cited column is a `count`, which reads no column.
    counts_rows: bool
