from __future__ import annotations

from typing import ClassVar

from app.core.ids import ID
from app.core.record import PersistedModel, PersistenceScope
from app.models.claims import (
    ClaimImportance,
    DataUniverseRequirement,
    PublishedCitation,
)


class ClaimShape(PersistedModel):
    collection: ClassVar[str] = "claim_shape"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project_id: ID
    # Authored before any stage exists, so it declares no stage and no column.
    label: str
    requires: DataUniverseRequirement
    importance: ClaimImportance


class Claim(PersistedModel):
    collection: ClassVar[str] = "claim"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    shape_id: ID
    # A deliverable is as often a table as a figure.
    citation: PublishedCitation
