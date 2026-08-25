"""What a project intends to claim, and what one run established."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel

from app.core.json_types import JsonScalar
from app.core.ids import ID


class ClaimImportance(str, Enum):
    primary = "primary"
    secondary = "secondary"


class DataUniverseRequirement(str, Enum):
    open = "open"
    equal_coverage = "equal_coverage"
    closed = "closed"



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


class StageOutputTableCitation(Citation):
    # A whole output frame, so no row and no column — row_count is what it published.
    kind: Literal["stage_output_table"] = "stage_output_table"
    run_id: ID
    stage_id: ID
    row_count: int


class StageOutputRowCitation(Citation):
    # A row pointed at with no value of its own — the show-the-work link.
    kind: Literal["stage_output_row"] = "stage_output_row"
    stage_id: ID
    row_ordinal: int

