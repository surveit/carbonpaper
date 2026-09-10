from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from app.core.errors import ClaimReviewIsImmutable
from app.core.ids import ID
from app.core.record import PersistedModel, PersistenceScope
from app.models.claim_review import Challenge, Grounding, Rewrite


class ClaimReview(PersistedModel):
    """A review is written once: a re-attack is a new claim and a new review."""

    collection: ClassVar[str] = "claim_review"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project_id: ID = Field(frozen=True)
    claim_id: ID = Field(frozen=True)
    run_id: ID = Field(frozen=True)
    # Every phrase of the claim, and what in the run it rests on.
    grounding: list[Grounding] = Field(frozen=True)
    challenges: list[Challenge] = Field(frozen=True)
    proposed_rewrites: list[Rewrite] = Field(default=[], frozen=True)
    summary: str = Field(frozen=True)
    # The attacker sessions, so a reader can open the transcript behind a challenge.
    session_ids: list[ID] = Field(default=[], frozen=True)

    def save(self) -> None:
        # Frozen fields stop a mutation; this stops a fresh record with a stored id.
        _validate_not_yet_stored(self)
        super().save()


def _validate_not_yet_stored(review: ClaimReview) -> None:
    if ClaimReview.load_or_none(review.id) is not None:
        raise ClaimReviewIsImmutable(review.id)
