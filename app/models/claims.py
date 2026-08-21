"""What a project intends to claim, and what one run established."""
from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import BaseModel

from app.core.persistence import PersistedModel, PersistenceScope
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


class StageCellCitation(BaseModel):
    stage_id: str
    # The column in the SOURCE, which need not share the name the shape declares.
    column: str
    value: str


class Claim(PersistedModel):
    collection: ClassVar[str] = "claim"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    shape_id: str
    run_id: str
    cites: StageCellCitation
