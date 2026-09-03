from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from app.core.errors import ClaimIsImmutable, ClaimShapeIsImmutable
from app.core.ids import ID
from app.core.record import PersistedModel, PersistenceScope
from app.core.json_types import JsonDict
from app.models.claims import (
    ClaimImportance,
    ClaimStatus,
    DataUniverseRequirement,
    PublishedCitation,
)
from app.models.schema import Column


class ClaimShape(PersistedModel):
    collection: ClassVar[str] = "claim_shape"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # Frozen: every claim under a shape asserts what it said.
    project_id: ID = Field(frozen=True)
    # Authored before any stage exists, so it declares no stage and no column.
    label: str = Field(frozen=True)
    universe: DataUniverseRequirement = Field(frozen=True)
    importance: ClaimImportance = Field(frozen=True)
    # Read these before using the number.
    qualifiers: list[str] = Field(default=[], frozen=True)
    # The axes a claim of this shape sits on, as ordinary columns.
    context: list[Column] = Field(default=[], frozen=True)
    # A suggestion, not an assertion, so it is the one thing here that may be rewritten.
    template: str = ""

    def save(self) -> None:
        _validate_only_the_template_moved(self)
        super().save()


class Claim(PersistedModel):
    collection: ClassVar[str] = "claim"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # Frozen: what a reader saw and what is stored can never disagree.
    created_by_project_id: ID = Field(frozen=True)
    shape_id: ID = Field(frozen=True)
    # One value per column the shape declares; matching contexts are one fact.
    context: JsonDict = Field(default={}, frozen=True)
    # What a person will publish. Frozen: review attacks these words, not later ones.
    text: str = Field(default="", frozen=True)
    # A deliverable is as often a table as a figure.
    citation: PublishedCitation = Field(frozen=True)
    # The one field that may move, and only through the review that decides it.
    status: ClaimStatus = ClaimStatus.submitted

    def save(self) -> None:
        # Frozen fields stop a mutation; this stops a fresh record with a stored id.
        _validate_only_the_status_moved(self)
        super().save()


_A_CLAIM_MAY_MOVE = {"status", "updated_at"}
_A_SHAPE_MAY_MOVE = {"template", "updated_at"}


def _validate_only_the_template_moved(shape: ClaimShape) -> None:
    held = ClaimShape.load_or_none(shape.id)
    if held is None:
        return
    if held.model_dump(exclude=_A_SHAPE_MAY_MOVE) != shape.model_dump(
        exclude=_A_SHAPE_MAY_MOVE
    ):
        raise ClaimShapeIsImmutable(shape.id)


def _validate_only_the_status_moved(claim: Claim) -> None:
    held = Claim.load_or_none(claim.id)
    if held is None:
        return
    if held.model_dump(exclude=_A_CLAIM_MAY_MOVE) != claim.model_dump(exclude=_A_CLAIM_MAY_MOVE):
        raise ClaimIsImmutable(claim.id)
