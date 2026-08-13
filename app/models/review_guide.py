"""A review guide as AUTHORED — what a human or agent writes. The stored record it
becomes is `app.services.versioning.ReviewGuide`, which adds the address.
"""
from __future__ import annotations

from pydantic import ConfigDict, Field

from app.models.schema import _Base
from app.models.tool_schema_prompts import (
    REVIEW_GUIDE_DRAFT_DESCRIPTION,
    REVIEW_GUIDE_STEP_DESCRIPTION,
)

# A step a journalist will actually skim is short.
PROSE_MAX_CHARS = 255
# The data sentence sits on a link, beside a measured shape, and wraps in a 360px rail.
DATA_DESCRIPTION_MAX_CHARS = 120
# The goal leads the same 360px rail, where a line holds roughly 60 characters: three
# lines is the most a reader takes in before the walkthrough, and past that it stops
# leading. It also stays under a step's own ceiling, so no goal outweighs the steps.
GOAL_MAX_CHARS = 180


class ReviewGuideStep(_Base):
    model_config = ConfigDict(json_schema_extra={"description": REVIEW_GUIDE_STEP_DESCRIPTION})


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
    # OPTIONAL HERE, and must stay so: every guide stored before this field existed
    # parses through PersistedModel.load's extra="forbid" model_validate, and a required
    # field would orphan all of them. Such a guide renders its data link with the size
    # alone — nothing is synthesised from the stage names to fill the gap.
    # WRITING a guide is where it is required: versioning.validate_review_guide refuses
    # one whose sections lack it, naming them, so no new guide can be stored without it.
    data_description: str | None = Field(
        default=None,
        max_length=DATA_DESCRIPTION_MAX_CHARS,
        description=(
            "One short sentence naming what the data LEAVING this section is — the rows "
            "themselves, not what the section did to them. 'Every filing both quarters "
            "reported.' 'The filings that named Venezuela, plus the ones the reporter "
            "listed by hand.' It is shown next to a link to the data -- it is what the "
            "reader uses to decide whether to open the full table and to ground what "
            "they will see."
        ),
    )


class ReviewGuideDraft(_Base):
    model_config = ConfigDict(json_schema_extra={"description": REVIEW_GUIDE_DRAFT_DESCRIPTION})


    # OPTIONAL HERE for the same reason `data_description` is: a guide stored before
    # this field existed parses through PersistedModel.load's extra="forbid"
    # model_validate, and a required field would orphan every one of them. Such a
    # guide leads with its first section — no sentence is invented to stand in.
    # WRITING a guide is where it is required: versioning.validate_review_guide
    # refuses one carrying no goal, naming it.
    goal: str | None = Field(
        default=None,
        max_length=GOAL_MAX_CHARS,
        description=(
            "One short sentence, in the second person, saying what the reader is here "
            "to decide: \"You're here to judge whether an organisation publicly "
            "committed to something and then lobbied against it.\" It names the "
            "JUDGEMENT, never the workflow's mechanics, and leads the walkthrough — "
            "nothing below it means anything until the reader has read it."
        ),
    )
    steps: list[ReviewGuideStep]
    unnarrated: list[str] = Field(default_factory=list)
