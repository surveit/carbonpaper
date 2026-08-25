"""What the Values used tab is handed, one entry per stage that wrote."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from app.models.schema import StageId
from app.web.stage_diff import RowAlignedDiff


class NewSheet(str, Enum):
    """A `replaces` stage hands back a new frame, and this is what it did to get one."""

    per_group = "per_group"
    one_row = "one_row"
    rebuilt = "rebuilt"


class SheetColumn(BaseModel):
    name: str
    cited: bool
    # The nearest stage upstream that wrote it; the header links there.
    writer: StageId | None


class ValuesStep(BaseModel):
    """One stage of the replay: its sheet, cut to the columns the walk passed through."""

    stage_id: StageId
    glyph: str
    label: str
    rows_total: int
    new_sheet: NewSheet | None
    columns: list[SheetColumn]
    # None where the diff refuses the type; `rows` below is then what is drawn.
    diff: RowAlignedDiff | None
    # Positional against `columns`, as text. Read only where `diff` is None.
    rows: list[list[str]]
    # Positional against `rows`: where each one sits in the stage's own frame.
    row_ordinals: list[int]
    # How many rows of this stage the figure came through, before the sheet's cap.
    rows_reached: int
    columns_total: int
    unreadable: str | None

    @property
    def column_writers(self) -> dict[str, StageId]:
        """What the diff header links: column name -> the stage that wrote it."""
        return {
            column.name: column.writer
            for column in self.columns
            if column.writer is not None
        }


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
    steps: list[ValuesStep]
    # Left to right, each entry one column of the minimap.
    minimap: list[list[MinimapNode]]
    edges: list[MinimapEdge]
    sources: dict[StageId, list[StepSource]]
    # Set where the cited column is a `count`, which reads no column.
    counts_rows: bool
