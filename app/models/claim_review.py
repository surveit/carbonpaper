"""A claim under review: what every reviewer is handed, and what each agent answers."""
from __future__ import annotations

from enum import Enum

from pydantic import Field

from app.core.file_shape import ColumnShape
from app.core.ids import ID
from app.core.json_types import JsonDict
from app.models.citations import CellCitation
from app.models.claims import ClaimShapeInput
from app.models.records.claim_review import ClaimPart, DraftChallenge
from app.models.records.workflow_output import WorkflowOutput
from app.models.records.workflow_version import Method
from app.models.schema import StageId, _Base


class Reviewer(str, Enum):
    data_defects = "data_defects"
    choices = "choices"
    omissions = "omissions"
    coverage = "coverage"
    meaning = "meaning"


# ── What each agent answers ──
class ChallengesAnswer(_Base):
    challenges: list[DraftChallenge] = Field(
        description="Everything you found worth raising; an empty list if the claim survived you."
    )


class DroppedChallenge(_Base):
    index: int = Field(
        ge=0, description="The challenge to drop, by its number in the list you were given.")
    duplicate_of: int = Field(
        ge=0, description="The challenge it repeats, by its number. Keep that one.")
    because: str = Field(
        description="What the two say that is the same thing, in one sentence.")


class DedupeAnswer(_Base):
    drop: list[DroppedChallenge] = Field(
        description="Every challenge that repeats another; an empty list if none does.")


class ClaimReviewResult(_Base):
    """What one review run produced: the merged answer, and every session behind it."""

    challenges: list[DraftChallenge]
    session_id: ID


def find_claim_part_spans(text: str, parts: list[ClaimPart]) -> list[tuple[int, int] | None]:
    return [_find_claim_part_span(text, part) for part in parts]


def _find_claim_part_span(text: str, part: ClaimPart) -> tuple[int, int] | None:
    # Each search starts one past the previous start, so "a a" is found twice in "a a a".
    start = -1
    for _ in range(part.occurrence):
        start = text.find(part.phrase, start + 1)
        if start == -1:
            return None
    return start, start + len(part.phrase)


# ── The evidence bundle ──
class StageEvidenceItem(_Base):
    stage_id: StageId
    type: str
    description: str
    input_ids: list[StageId]
    code: str
    feeds_the_cited_stage: bool


class BranchEvidenceItem(_Base):
    branch_id: str
    stage_id: StageId
    reason: str
    label: str
    source_code: str
    # 0 is a count: the arm no row took. Every option here comes from an enumerated stage.
    rows_count: int


class InputColumnEvidenceItem(_Base):
    stage_id: ID
    row_count: int
    shape: ColumnShape


# The page text either side of a cited quote, as the claim's run read the page.
class CitedPassage(_Base):
    quote: str
    locator_label: str
    before: str
    after: str


class EvidenceBundle(_Base):
    claim_id: ID
    claim_text: str
    claim_context: JsonDict
    # The run is the cited cell's run.
    cited: CellCitation
    # None where the cited cell holds a value, not a span.
    cited_passage: CitedPassage | None
    shape: ClaimShapeInput
    run_read_everything: bool
    outputs: list[WorkflowOutput]
    cited_slug: str
    stages: list[StageEvidenceItem]
    branches: list[BranchEvidenceItem]
    input_columns: list[InputColumnEvidenceItem]
    # None: the run's version was saved before a version kept the project's method.
    method: Method | None
