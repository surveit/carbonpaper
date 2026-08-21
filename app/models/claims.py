"""What a project intends to claim, declared with the methodology before any stage exists."""
from __future__ import annotations

from enum import Enum
from typing import ClassVar

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
