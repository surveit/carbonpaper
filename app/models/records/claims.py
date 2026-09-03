from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from app.core.errors import ClaimIsImmutable, ClaimShapeIsImmutable
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

    # Frozen: every claim under a shape asserts what it said.
    project_id: ID = Field(frozen=True)
    # Authored before any stage exists, so it declares no stage and no column.
    label: str = Field(frozen=True)
    requires: DataUniverseRequirement = Field(frozen=True)
    importance: ClaimImportance = Field(frozen=True)
    # Read these before using the number.
    qualifiers: list[str] = Field(default=[], frozen=True)

    def save(self) -> None:
        if ClaimShape.exists(self.id):
            raise ClaimShapeIsImmutable(self.id)
        super().save()


class Claim(PersistedModel):
    collection: ClassVar[str] = "claim"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # Frozen: what a reader saw and what is stored can never disagree.
    created_by_project_id: ID = Field(frozen=True)
    shape_id: ID = Field(frozen=True)
    # A deliverable is as often a table as a figure.
    citation: PublishedCitation = Field(frozen=True)

    def save(self) -> None:
        # The frozen fields stop a mutation; this stops a fresh record with a stored id.
        if Claim.exists(self.id):
            raise ClaimIsImmutable(self.id)
        super().save()
