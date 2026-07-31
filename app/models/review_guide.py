"""A review guide as AUTHORED — what a human or agent writes. The stored record it
becomes is `app.services.versioning.ReviewGuide`, which adds the address.
"""
from __future__ import annotations

from pydantic import Field

from app.models.schema import _Base

# A step a journalist will actually skim is short.
PROSE_MAX_CHARS = 255


class ReviewGuideStep(_Base):
    """One step of the walkthrough. `prose` may carry `backticked` column names."""

    title: str
    prose: str = Field(
        max_length=PROSE_MAX_CHARS,
        description=(
            "One or two plain sentences saying what this step does to the data. A "
            "caution belongs here only where a human made an editorial choice the "
            "reader could disagree with."
        ),
    )
    stage_ids: list[str]


class ReviewGuideDraft(_Base):
    """A guide as written, before it is addressed to a version and stored."""

    steps: list[ReviewGuideStep]
    unnarrated: list[str] = Field(default_factory=list)
