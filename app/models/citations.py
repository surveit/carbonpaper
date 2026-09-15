"""Where a claimed or published value sits in a run, and what a publish stage said about it."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from app.core.json_types import JsonScalar
from app.core.ids import ID


class CitedValue(BaseModel):
    stage_id: ID
    row_ordinal: int
    column: str
    label: str  # what the artifact calls this value
    value: str  # the cell, checked against the row before this was recorded


class Citation(BaseModel):
    """Where the evidence sits. The kind decides what else a citation carries."""

    kind: str


class StageOutputCellCitation(Citation):
    kind: Literal["stage_output_cell"] = "stage_output_cell"
    run_id: ID = Field(description="The run, as the evidence pool names it.")
    stage_id: ID = Field(description="The stage, as the evidence pool names it.")
    row_ordinal: int = Field(description="The row's position in the stage output, counting from 0.")
    column: str = Field(description="The column's name, spelled as the evidence pool spells it.")
    value: JsonScalar = Field(description="The cell's value, copied exactly as the evidence pool prints it.")


class RowsRectangle(BaseModel):
    """A table inside a stage's output: rows [row_start, row_end) by name-ordered columns."""

    row_start: int
    row_end: int
    columns: list[str]

    def count_rows(self) -> int:
        return self.row_end - self.row_start

    def is_whole_output(self, columns: list[str], row_count: int) -> bool:
        # By set: a reordering cuts no cell, and the question here is what was cut.
        return (
            self.row_start == 0
            and self.row_end == row_count
            and set(self.columns) == set(columns)
        )


class StageOutputTableCitation(Citation):
    # The rows and columns actually published, never the whole frame by assumption.
    kind: Literal["stage_output_table"] = "stage_output_table"
    run_id: ID
    stage_id: ID
    rectangle: RowsRectangle


PublishedCitation = Annotated[
    Union[StageOutputCellCitation, StageOutputTableCitation], Field(discriminator="kind")
]


class StageOutputRowCitation(Citation):
    # A row pointed at with no value of its own — the show-the-work link.
    kind: Literal["stage_output_row"] = "stage_output_row"
    stage_id: ID
    row_ordinal: int


class StageOutputColumnCitation(Citation):
    kind: Literal["stage_output_column"] = "stage_output_column"
    stage_id: ID = Field(description="The stage, as the evidence pool names it.")
    column: str = Field(description="The column's name, spelled as the evidence pool spells it.")


class StageCitation(Citation):
    kind: Literal["stage"] = "stage"
    stage_id: ID = Field(description="The stage, as the evidence pool names it.")


class TermCitation(Citation):
    kind: Literal["term"] = "term"
    name: str = Field(description="The defined term, exactly as the terms name it.")


ChallengeCitation = Annotated[
    Union[StageOutputCellCitation, StageOutputColumnCitation, StageCitation, TermCitation],
    Field(discriminator="kind"),
]
