"""What a project intends to claim, and what one run established."""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from app.core.json_types import JsonScalar
from app.core.ids import ID
from app.models.schema import _Base


class ClaimImportance(str, Enum):
    primary = "primary"
    secondary = "secondary"


class DataUniverseRequirement(str, Enum):
    open = "open"
    equal_coverage = "equal_coverage"
    closed = "closed"


# What each requirement asserts, in the words a reader of the figure gets.
DATA_UNIVERSE_PROSE = {
    "open": "at least these — more may exist",
    "equal_coverage": "the same ground as what it is compared against",
    "closed": "this is all of them",
}



class AuthoredClaimShape(_Base):
    """One entry of what a project claims, as written. `id` names a shape already stored."""

    id: ID | None = None
    label: str
    requires: DataUniverseRequirement
    importance: ClaimImportance


class Citation(BaseModel):
    """Where the evidence sits. The kind decides what else a citation carries."""

    kind: str


class StageOutputCellCitation(Citation):
    kind: Literal["stage_output_cell"] = "stage_output_cell"
    run_id: ID
    stage_id: ID
    row_ordinal: int
    column: str
    value: JsonScalar


class RowsRectangle(BaseModel):
    """A table inside a stage's output: rows [row_start, row_end) by name-ordered columns."""

    row_start: int
    row_end: int
    columns: list[str]

    def count_rows(self) -> int:
        return self.row_end - self.row_start


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

