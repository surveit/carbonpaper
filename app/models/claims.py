"""What a project intends to claim, and what one run established."""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from app.core.json_types import JsonScalar
from app.core.ids import ID
from app.models.schema import Column, _Base


class ClaimImportance(str, Enum):
    primary = "primary"
    secondary = "secondary"


class ClaimStatus(str, Enum):
    """A claim is proposed, then stood behind or refused. The only field that may move."""

    submitted = "submitted"
    approved = "approved"
    declined = "declined"


class DataUniverseRequirement(str, Enum):
    open = "open"
    closed = "closed"


# The word is the fact and goes on the page; this explains it on hover.
DATA_UNIVERSE_TOOLTIP = {
    "open": "The dataset holds only the events it captured, so this figure is a floor: "
            "the real number is AT LEAST this.",
    "closed": "The dataset holds every event of this kind, so this figure is the total: "
              "it IS this number.",
}



class ClaimShapeInput(_Base):
    """What a caller sends to author one shape. A stored shape is never edited, so no id."""

    label: str
    requires: DataUniverseRequirement
    importance: ClaimImportance
    qualifiers: list[str] = []
    # The axes a claim of this shape sits on, as ordinary columns.
    context: list[Column] = []


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

