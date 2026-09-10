"""A claim under attack: what every attacker is handed, what each returns, and what is stored."""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import Field

from app.core.column_profile import ValueCount
from app.core.ids import ID
from app.core.json_types import JsonDict
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId, _Base


class Attacker(str, Enum):
    grounding = "grounding"
    data_defects = "data_defects"
    choices = "choices"
    omissions = "omissions"
    coverage = "coverage"
    meaning = "meaning"
    orchestrator = "orchestrator"


class ChallengeKind(str, Enum):
    data = "data"
    choice = "choice"
    omission = "omission"
    coverage = "coverage"
    semantic = "semantic"
    gap = "gap"


class Moves(str, Enum):
    moves = "moves"
    meaning = "meaning"
    unpriced = "unpriced"
    none = "none"


class Cost(str, Enum):
    free = "free"
    person = "person"
    outside = "outside"
    editorial = "editorial"
    settled = "settled"


SEVERITY_FLOOR = 0
SEVERITY_CEILING = 3

SEVERITY_WORDS: dict[int, str] = {
    0: "checked, quiet",
    1: "worth a footnote",
    2: "moves the figure, or bounds it",
    3: "could not stand as written",
}

_SEVERITY_DESCRIPTION = "How far it moves the claim. " + ". ".join(
    f"{level} {word}" for level, word in SEVERITY_WORDS.items()
)

_EVIDENCE_REFS_DESCRIPTION = (
    "The pieces of the run your evidence reads, so a reader can open them."
)


# ── Evidence refs ──
class OutputEvidence(_Base):
    kind: Literal["output"] = "output"
    slug: str = Field(description="The slug of the output figure, as the bundle lists it.")


class InputColumnEvidence(_Base):
    kind: Literal["input_column"] = "input_column"
    stage_id: StageId = Field(description="The stage reading the column.")
    column: str = Field(description="The column's name, spelled as the bundle spells it.")


class StageEvidence(_Base):
    kind: Literal["stage"] = "stage"
    stage_id: StageId = Field(description="The stage whose code or description carries the phrase.")


class BranchEvidence(_Base):
    kind: Literal["branch"] = "branch"
    branch_id: str = Field(description="The branch that kept or dropped the rows.")


class TermEvidence(_Base):
    kind: Literal["term"] = "term"
    name: str = Field(description="The defined term, exactly as the methodology names it.")


EvidenceRef = Annotated[
    Union[OutputEvidence, InputColumnEvidence, StageEvidence, BranchEvidence, TermEvidence],
    Field(discriminator="kind"),
]


# ── What the review holds ──
class Grounding(_Base):
    start: int = Field(ge=0, description="Where the phrase starts in the claim, counting characters from 0.")
    end: int = Field(gt=0, description="Where the phrase ends: the character after its last one.")
    evidence: EvidenceRef | None = Field(
        description="The one thing in the run the phrase rests on, or null if it rests on nothing."
    )
    how: str = Field(description="One line: how the phrase rests on that piece of the run.")


class Challenge(_Base):
    attacker: Attacker = Field(description="Which attacker raised it.")
    kind: ChallengeKind = Field(description="What sort of trouble this is.")
    grounding_index: int | None = Field(
        default=None, ge=0,
        description="Which phrase it lands on, by position in the groundings; null for the whole sentence.",
    )
    text: str = Field(description="The challenge in one sentence, addressed to the journalist.")
    evidence: str = Field(description="What in the run makes it stick, in one sentence.")
    backing: str = Field(description="A figure or phrase copied word for word from the run.")
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list, description=_EVIDENCE_REFS_DESCRIPTION
    )
    severity: int = Field(
        ge=SEVERITY_FLOOR, le=SEVERITY_CEILING, description=_SEVERITY_DESCRIPTION,
    )
    moves: Moves = Field(description="What answering it would move: the figure, its meaning, nothing priced, or nothing.")
    cost: Cost = Field(description="What answering it would take: nothing, a person, an outside source, an editorial call, or it is settled.")
    raised_by: str = Field(default="", description="The attacker's own words for who or what prompted it; empty if nothing did.")


class Rewrite(_Base):
    text: str = Field(description="The claim rewritten as one sentence the run can carry.")
    why: str = Field(description="One line: what this wording fixes.")


# ── What each attacker submits ──
class GroundingAnswer(_Base):
    phrases: list[Grounding] = Field(
        description="Every phrase of the claim that asserts something, in the order it is read."
    )


class RaisedChallenge(_Base):
    kind: ChallengeKind = Field(description="What sort of trouble this is.")
    grounding_index: int | None = Field(
        default=None, ge=0,
        description="Which phrase it lands on, by position in the groundings; null for the whole sentence.",
    )
    text: str = Field(description="The challenge in one sentence, addressed to the journalist.")
    evidence: str = Field(description="What in the run makes it stick, in one sentence.")
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list, description=_EVIDENCE_REFS_DESCRIPTION
    )
    moves: Moves = Field(description="What answering it would move: the figure, its meaning, nothing priced, or nothing.")
    cost: Cost = Field(description="What answering it would take: nothing, a person, an outside source, an editorial call, or it is settled.")
    raised_by: str = Field(default="", description="Who or what prompted it, in your own words; empty if nothing did.")


class ChallengesAnswer(_Base):
    challenges: list[RaisedChallenge] = Field(
        description="Everything you found worth raising; an empty list if the claim survived you."
    )


class MeaningAnswer(ChallengesAnswer):
    rewrites: list[Rewrite] = Field(
        default_factory=list, max_length=2,
        description="At most two rewordings the run can carry; none if the claim already reads right.",
    )


class AttackerAnswers(_Base):
    grounding: GroundingAnswer
    data_defects: ChallengesAnswer
    choices: ChallengesAnswer
    omissions: ChallengesAnswer
    coverage: ChallengesAnswer
    meaning: MeaningAnswer

    def list_evidence(self) -> list[str]:
        raised = [self.data_defects, self.choices, self.omissions, self.coverage, self.meaning]
        return [challenge.evidence for answer in raised for challenge in answer.challenges]


class ClaimReviewDraft(_Base):
    challenges: list[Challenge] = Field(
        description="The challenges you kept, each with its attacker, severity and backing filled in."
    )
    summary: str = Field(description="What the claim can stand as, in one paragraph the journalist reads first.")


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
