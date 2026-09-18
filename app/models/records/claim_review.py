"""A stored claim review: the claim's parts, the challenges kept, and the sessions behind them."""
from __future__ import annotations

from enum import Enum, IntEnum
from typing import ClassVar

from pydantic import Field, model_validator

from app.core.errors import ClaimReviewIsImmutable
from app.core.ids import ID
from app.core.record import PersistedModel, PersistenceScope
from app.models.citations import AddressedChallengeCitation, ChallengeCitation
from app.models.schema import _Base


class ChallengeKind(str, Enum):
    data = "data"
    choice = "choice"
    omission = "omission"
    coverage = "coverage"
    meaning = "meaning"
    gap = "gap"


# Ordered: the page folds the quiet ones behind a count and sorts the rest by weight.
class Severity(IntEnum):
    info = 0
    low = 1
    high = 2
    critical = 3


# The name is the label; the words are what a reader is shown on hovering it.
SEVERITY_WORDS: dict[Severity, str] = {
    Severity.info: "nothing moves; a choice was made a reader should know about",
    Severity.low: "the figure moves, but not materially",
    Severity.high: "the claim's shape holds, but the figure moves materially",
    Severity.critical: "a reader draws a conclusion the run does not support",
}

_SEVERITY_DESCRIPTION = "How wrong a reader is left. " + ". ".join(
    f"{level.value} {word}" for level, word in SEVERITY_WORDS.items()
)


class ClaimPart(_Base):
    phrase: str = Field(min_length=1, description="The phrase, copied word for word from the claim.")
    occurrence: int = Field(
        default=1, ge=1, description="Which time the phrase appears in the claim, counting from 1."
    )


# What a reviewer answers: no project, which it cannot read off the evidence pool.
class DraftChallenge(_Base):
    kind: ChallengeKind = Field(description="What sort of stretch this is.")
    claim_part: ClaimPart | None = Field(
        default=None,
        description="The phrase of the claim it lands on; null when it is about the whole sentence.",
    )
    text: str = Field(description="The challenge in one sentence, addressed to the claim owner.")
    justification: str = Field(description="What in the run makes it stick, in one sentence.")
    citations: list[ChallengeCitation] = Field(
        default_factory=list,
        description="The pieces of the run it rests on, so a reader can open every one.",
    )
    severity: Severity = Field(description=_SEVERITY_DESCRIPTION)

    @model_validator(mode="after")
    def _validate_cited(self) -> DraftChallenge:
        # A gap IS the absence of footing, so it is the one kind with nothing to point at.
        if self.kind != ChallengeKind.gap and not self.citations:
            raise ValueError(f"a {self.kind} challenge cites nothing in the run")
        return self


class Challenge(_Base):
    """The stored one: every citation stamped with the project, so it opens on its own."""

    kind: ChallengeKind
    claim_part: ClaimPart | None = None
    text: str
    justification: str
    citations: list[AddressedChallengeCitation] = Field(default_factory=list)
    severity: Severity


class ClaimReview(PersistedModel):
    """A review is written once: a re-review is a new claim and a new review."""

    collection: ClassVar[str] = "claim_review"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    claim_id: ID = Field(frozen=True)
    challenges: list[Challenge] = Field(frozen=True)
    # The one session the review ran under, so a reader can open what it spent.
    session_id: ID = Field(frozen=True)

    def save(self) -> None:
        # Frozen fields stop a mutation; this stops a fresh record with a stored id.
        _validate_not_yet_stored(self)
        super().save()


def _validate_not_yet_stored(review: ClaimReview) -> None:
    if ClaimReview.load_or_none(review.id) is not None:
        raise ClaimReviewIsImmutable(review.id)
