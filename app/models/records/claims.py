from __future__ import annotations

from typing import ClassVar

from app.core.errors import ClaimIsImmutable
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
    # Read these before using the number.
    qualifiers: list[str] = []


class Claim(PersistedModel):
    collection: ClassVar[str] = "claim"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # A project makes a claim; it does not own one, and a reader outside the project reads it.
    created_by_project_id: ID
    shape_id: ID
    # Stored, not read off the run: a claim outlives its run's manifest.
    workflow_version_id: ID
    # A deliverable is as often a table as a figure.
    citation: PublishedCitation

    def save(self) -> None:
        # frozen=True cannot express this: save() stamps updated_at.
        if Claim.exists(self.id):
            raise ClaimIsImmutable(self.id)
        super().save()
