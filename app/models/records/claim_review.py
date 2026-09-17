"""A stored claim review: the claim's parts, the challenges kept, and the sessions behind them."""
from __future__ import annotations

from enum import Enum, IntEnum
from typing import ClassVar

from pydantic import Field, model_validator

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


class Severity(IntEnum):
    """Ordered: the page folds the quiet ones behind a count and sorts the rest by weight."""

    noted = 0
    minor = 1
    major = 2
    misleading = 3


SEVERITY_WORDS: dict[Severity, str] = {
    Severity.noted: "nothing moves; a choice was made the reader should know about",
    Severity.minor: "the figure moves, not by enough to change anything",
    Severity.major: "the claim's shape holds, but the figure moves enough to change what it means",
    Severity.misleading: "the reader draws a conclusion the run does not support",
}

_SEVERITY_DESCRIPTION = "How wrong the reader is left. " + ". ".join(
    f"{level.value} {word}" for level, word in SEVERITY_WORDS.items()
)


class ClaimPart(_Base):
    phrase: str = Field(min_length=1, description="The phrase, copied word for word from the claim.")
    occurrence: int = Field(
        default=1, ge=1, description="Which time the phrase appears in the claim, counting from 1."
    )


class Challenge(_Base):
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
    def _validate_cited(self) -> Challenge:
        # A gap IS the absence of footing, so it is the one kind with nothing to point at.
        if self.kind != ChallengeKind.gap and not self.citations:
            raise ValueError(f"a {self.kind} challenge cites nothing in the run")
        return self


class ClaimReview(PersistedModel):
    """A review is written once: a re-review is a new claim and a new review."""

    collection: ClassVar[str] = "claim_review"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    claim_id: ID = Field(frozen=True)
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
