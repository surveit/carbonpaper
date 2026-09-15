"""A stored claim review: the claim's parts, the challenges kept, and the sessions behind them."""
from __future__ import annotations

from enum import Enum
from typing import ClassVar

from pydantic import Field

from app.core.errors import ClaimReviewIsImmutable
from app.core.ids import ID
from app.core.record import PersistedModel, PersistenceScope
from app.models.citations import ChallengeCitation
from app.models.schema import _Base


class ChallengeKind(str, Enum):
    data = "data"
    choice = "choice"
    omission = "omission"
    coverage = "coverage"
    meaning = "meaning"
    gap = "gap"


SEVERITY_FLOOR = 0
SEVERITY_CEILING = 3

SEVERITY_WORDS: dict[int, str] = {
    0: "checked and does not hurt the claim, but the assumption is worth noting",
    1: "low-probability or low-magnitude risks to the number or its framing",
    2: "the claim's structure is right, but its value risks a meaningful deviation",
    3: "actively misleading: it will lead readers to incorrect conclusions",
}

_SEVERITY_DESCRIPTION = "How far it moves the claim. " + ". ".join(
    f"{level} {word}" for level, word in SEVERITY_WORDS.items()
)


class ClaimPart(_Base):
    phrase: str = Field(min_length=1, description="The phrase, copied word for word from the claim.")
    occurrence: int = Field(
        default=1, ge=1, description="Which time the phrase appears in the claim, counting from 1."
    )


class Challenge(_Base):
    kind: ChallengeKind = Field(description="What sort of stretch this is.")
    claim_part_index: int | None = Field(
        default=None, ge=0,
        description="Which claim part it lands on, by position in the claim parts; null for the whole sentence.",
    )
    text: str = Field(description="The challenge in one sentence, addressed to the claim owner.")
    justification: str = Field(description="What in the run makes it stick, in one sentence.")
    evidence: str = Field(description="A figure or phrase copied word for word from the run.")
    citations: list[ChallengeCitation] = Field(
        default_factory=list,
        description="The pieces of the run the evidence sits in, so a reader can open them.",
    )
    severity: int = Field(
        ge=SEVERITY_FLOOR, le=SEVERITY_CEILING, description=_SEVERITY_DESCRIPTION,
    )


class ClaimReview(PersistedModel):
    """A review is written once: a re-review is a new claim and a new review."""

    collection: ClassVar[str] = "claim_review"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    claim_id: ID = Field(frozen=True)
    claim_parts: list[ClaimPart] = Field(frozen=True)
    challenges: list[Challenge] = Field(frozen=True)
    summary: str = Field(frozen=True)
    # Every review session in the order it ran, so a reader can open the transcripts.
    session_ids: list[ID] = Field(frozen=True)

    def save(self) -> None:
        # Frozen fields stop a mutation; this stops a fresh record with a stored id.
        _validate_not_yet_stored(self)
        super().save()


def _validate_not_yet_stored(review: ClaimReview) -> None:
    if ClaimReview.load_or_none(review.id) is not None:
        raise ClaimReviewIsImmutable(review.id)
