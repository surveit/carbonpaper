"""A claim under review: what every reviewer is handed, and what each agent answers."""
from __future__ import annotations

from pydantic import Field

from app.core.column_profile import ValueCount
from app.core.ids import ID
from app.core.json_types import JsonDict
from app.models.citations import StageOutputCellCitation
from app.models.records.claim_review import Challenge, ClaimPart
from app.models.schema import StageId, _Base


# ── What each agent answers ──
class ClaimPartsAnswer(_Base):
    claim_parts: list[ClaimPart] = Field(
        description="Every phrase of the claim that asserts something, in the order it is read."
    )


class ChallengesAnswer(_Base):
    challenges: list[Challenge] = Field(
        description="Everything you found worth raising; an empty list if the claim survived you."
    )


class OrchestratorAnswer(_Base):
    challenges: list[Challenge] = Field(
        description="The challenges you kept, each with its severity ruled."
    )
    summary: str = Field(
        description="What the claim can stand as, in one paragraph the claim owner reads first."
    )


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
class CitedShape(_Base):
    label: str
    universe: str
    importance: str
    qualifiers: list[str]
    context_columns: list[str]


class OutputEvidenceItem(_Base):
    slug: str
    label: str
    primary: bool
    stage_id: StageId
    value: str
    cited: bool


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
    role: str
    label: str
    source_code: str
    # 0 is a count: the arm no row took. Every option here comes from an enumerated stage.
    rows_count: int


class InputColumnEvidenceItem(_Base):
    stage_id: StageId
    column: str
    kind: str
    row_count: int
    filled_count: int
    null_count: int
    blank_count: int
    distinct_count: int
    top: list[ValueCount]


class EvidenceBundle(_Base):
    project_id: ID
    run_id: ID
    claim_id: ID
    claim_text: str
    claim_context: JsonDict
    cited: StageOutputCellCitation
    shape: CitedShape
    run_read_everything: bool
    outputs: list[OutputEvidenceItem]
    stages: list[StageEvidenceItem]
    branches: list[BranchEvidenceItem]
    input_columns: list[InputColumnEvidenceItem]
    terms: str
    methodology: str | None
