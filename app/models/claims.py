"""What a project intends to claim, and what one run established."""
from __future__ import annotations

from enum import Enum
from typing import ClassVar, Literal

from pydantic import BaseModel

from app.core.persistence import JsonScalar, PersistedModel, PersistenceScope
from app.models.schema import TableSchema
from app.core.ids import ID


class ClaimImportance(str, Enum):
    primary = "primary"
    secondary = "secondary"


class DataUniverseRequirement(str, Enum):
    open = "open"
    equal_coverage = "equal_coverage"
    closed = "closed"


class ClaimShape(PersistedModel):
    collection: ClassVar[str] = "claim_shape"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project_id: ID
    # Authored before any stage exists, so it declares no stage and no column.
    label: str
    table_schema: TableSchema
    requires: DataUniverseRequirement
    importance: ClaimImportance


class Citation(BaseModel):
    """Where the evidence sits. The kind decides what else a citation carries."""

    kind: str


class StageOutputCellCitation(Citation):
    kind: Literal["stage_output_cell"] = "stage_output_cell"
    stage_id: ID
    row_ordinal: int
    # The column in the SOURCE, which need not share the name the shape declares.
    column: str
    value: JsonScalar


class StageOutputRowCitation(Citation):
    # A row pointed at with no value of its own — the show-the-work link.
    kind: Literal["stage_output_row"] = "stage_output_row"
    stage_id: ID
    row_ordinal: int


class Claim(PersistedModel):
    collection: ClassVar[str] = "claim"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    shape_id: ID
    run_id: ID
    citation: StageOutputCellCitation
