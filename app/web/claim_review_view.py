"""What the claim page draws, and the state of the attack behind it."""
from __future__ import annotations

from collections import Counter

from pydantic import BaseModel

from app.core.agent.store import AgentSession, SessionStore
from app.core.ids import ID
from app.models.claim_review import (
    SEVERITY_WORDS,
    Attacker,
    BranchEvidence,
    Challenge,
    ChallengeKind,
    Cost,
    EvidenceRef,
    Grounding,
    InputColumnEvidence,
    Moves,
    OutputEvidence,
    StageEvidence,
    TermEvidence,
)
from app.models.claims import PublishedCitation, StageOutputCellCitation
from app.models.records.claim_review import ClaimReview
from app.models.records.claims import Claim, ClaimShape
from app.runtime.citations import build_row_trace_url
from app.services import claim_evidence
from app.services import claims as claims_service
from app.services.claim_review import find_attack_sessions, load_claim_review
from app.services.claim_shapes import load_claim_shape
from app.services.errors import ClaimRefused
from app.services.generation import GENERATION_FAILURE_PREFIX
from app.services.run import read_run_manifest
from app.web.claims_view import describe_what_blocks_the_run
from app.web.run_index import RunIndexRow, build_run_index_rows


class SentenceToken(BaseModel):
    text: str
    grounding_index: int | None = None
    severity: int | None = None
    # A span the review grounds nothing on; a stored review has none.
    unread: bool = False


class GroundRow(BaseModel):
    phrase: str
    lands_on: str | None
    how: str


class ChallengeCard(BaseModel):
    phrase: str
    attacker: str
    kind: str
    kind_words: str
    severity: int
    severity_words: str
    text: str
    evidence: str
    backing: str
    moves: str
    moves_words: str
    cost_words: str
    raised_by: str


class RewriteCard(BaseModel):
    text: str
    why: str


class AttackerWords(BaseModel):
    attacker: Attacker
    name: str
    reads: str
    does: str


class AttackerCount(BaseModel):
    name: str
    reads: str
    does: str
    count: int


class OutputRow(BaseModel):
    slug: str
    label: str
    value: str
    href: str
    cited: bool


class ClaimReviewPage(BaseModel):
    claim_id: ID
    run_id: ID
    run_href: str
    status: str
    text: str
    value: str
    value_href: str
    shape_label: str
    universe: str
    qualifiers: list[str]
    context: list[str]
    run_read_everything: bool
    blocked: str
    outputs: list[OutputRow]
    attack: str
    attack_error: str | None
    attack_session_id: ID | None
    tokens: list[SentenceToken]
    ground: list[GroundRow]
    open_challenges: list[ChallengeCard]
    quiet_challenges: list[ChallengeCard]
    rewrites: list[RewriteCard]
    summary: str
    attackers: list[AttackerCount]
    session_ids: list[ID]


ATTACK_NONE = "none"
ATTACK_RUNNING = "running"
ATTACK_FAILED = "failed"
ATTACK_DONE = "done"
ATTACK_REFUSED = "refused"

KIND_WORDS: dict[ChallengeKind, str] = {
    ChallengeKind.data: "data",
    ChallengeKind.choice: "a choice made",
    ChallengeKind.omission: "a decision never made",
    ChallengeKind.coverage: "coverage",
    ChallengeKind.semantic: "what it means",
    ChallengeKind.gap: "not examined",
}

MOVES_WORDS: dict[Moves, str] = {
    Moves.moves: "moves the figure",
    Moves.meaning: "changes the sentence",
    Moves.unpriced: "cannot be told yet",
    Moves.none: "does not move it",
}

COST_WORDS: dict[Cost, str] = {
    Cost.free: "free rerun",
    Cost.person: "a person's call",
    Cost.outside: "outside data",
    Cost.editorial: "editorial",
    Cost.settled: "settled in the methodology",
}

ATTACKER_WORDS: list[AttackerWords] = [
    AttackerWords(
        attacker=Attacker.grounding, name="Grounding",
        reads="the sentence against the pool",
        does="lands every load-bearing phrase on a figure, column, term or stage, "
             "or on nothing"),
    AttackerWords(
        attacker=Attacker.data_defects, name="Data defects",
        reads="the input files and how each stage reads them",
        does="malformed cells, two spellings of one thing, a reading that disagrees "
             "with its source"),
    AttackerWords(
        attacker=Attacker.choices, name="Choices made",
        reads="stage code and the recorded arms",
        does="every threshold, field and cut, swept to its alternatives"),
    AttackerWords(
        attacker=Attacker.omissions, name="Decisions never made",
        reads="input columns against the recorded arms",
        does="a column that partitions the rows and that no branch reads"),
    AttackerWords(
        attacker=Attacker.coverage, name="Coverage",
        reads="filters' dropped rows and the shape's open/closed word",
        does="who is in the count, who is not, and what the file does not hold"),
    AttackerWords(
        attacker=Attacker.meaning, name="Meaning",
        reads="the sentence, the stage descriptions and the terms",
        does="the same number read as a different sentence, and the rewrites the run "
             "also supports"),
]


def build_claim_review_page(project_id: ID, claim_id: ID) -> ClaimReviewPage:
    claim = claims_service.load_claim(project_id, claim_id)
    review = load_claim_review(project_id, claim_id)
    run = _read_run_row(project_id, claim.citation.run_id)
    shape = _read_shape(project_id, claim)
    return _build_page(project_id, claim, shape, run, review)


# ── the page ─────


def _build_page(project_id: ID, claim: Claim, shape: ClaimShape, run: RunIndexRow,
                review: ClaimReview | None) -> ClaimReviewPage:
    attack = _read_attack(claim, review)
    return ClaimReviewPage(
        claim_id=claim.id,
        run_id=claim.citation.run_id,
        run_href=f"/project/{project_id}/runs/{claim.citation.run_id}",
        status=claim.status,
        text=claim.text,
        value=claim_evidence.read_output_value(claim.citation),
        value_href=_build_citation_href(project_id, claim.citation),
        shape_label=shape.label,
        universe=shape.universe,
        qualifiers=list(shape.qualifiers),
        context=[f"{name} {value}" for name, value in claim.context.items()],
        run_read_everything=_read_whether_the_run_read_everything(project_id, claim),
        blocked=describe_what_blocks_the_run(run),
        outputs=_build_outputs(project_id, claim),
        attack=attack.attack,
        attack_error=attack.error,
        attack_session_id=attack.session_id,
        tokens=_build_tokens(claim.text, review),
        ground=_build_ground(claim.text, review),
        open_challenges=_build_open_challenges(claim.text, review),
        quiet_challenges=_build_quiet_challenges(claim.text, review),
        rewrites=_build_rewrites(review),
        summary=review.summary if review is not None else "",
        attackers=_count_attackers(review),
        session_ids=list(review.session_ids) if review is not None else [],
    )


def _read_whether_the_run_read_everything(project_id: ID, claim: Claim) -> bool:
    manifest = read_run_manifest(project_id, claim.citation.run_id)
    return claims_service.read_whether_the_run_read_everything(manifest)


def _build_outputs(project_id: ID, claim: Claim) -> list[OutputRow]:
    run_id = claim.citation.run_id
    cited = claims_service.find_output_of_claim(claim)
    return [
        OutputRow(
            slug=output.slug, label=output.label, value=output.value, cited=output.cited,
            href=build_row_trace_url(project_id, run_id, output.stage_id, 0))
        for output in claim_evidence.read_outputs(run_id, cited.slug)
    ]


def _build_citation_href(project_id: ID, citation: PublishedCitation) -> str:
    if isinstance(citation, StageOutputCellCitation):
        return build_row_trace_url(project_id, citation.run_id, citation.stage_id,
                                   citation.row_ordinal, column=citation.column)
    return build_row_trace_url(project_id, citation.run_id, citation.stage_id,
                               citation.rectangle.row_start)


# ── the sentence and its ground ─────


def _build_tokens(text: str, review: ClaimReview | None) -> list[SentenceToken]:
    grounding = _read_grounding(review)
    challenges = _read_challenges(review)
    tokens: list[SentenceToken] = []
    reached = 0
    for index in _read_phrase_order(grounding):
        phrase = grounding[index]
        _append_plain(tokens, text[reached:phrase.start])
        tokens.append(SentenceToken(
            text=text[phrase.start:phrase.end], grounding_index=index,
            severity=_find_worst_severity(challenges, index)))
        reached = phrase.end
    _append_plain(tokens, text[reached:])
    return tokens


def _build_ground(text: str, review: ClaimReview | None) -> list[GroundRow]:
    grounding = _read_grounding(review)
    return [
        GroundRow(
            phrase=text[grounding[index].start:grounding[index].end],
            lands_on=_describe_evidence_ref(grounding[index].evidence),
            how=grounding[index].how)
        for index in _read_phrase_order(grounding)
    ]


def _describe_evidence_ref(evidence: EvidenceRef | None) -> str | None:
    if evidence is None:
        return None
    if isinstance(evidence, OutputEvidence):
        return f"output {evidence.slug}"
    if isinstance(evidence, InputColumnEvidence):
        return f"column {evidence.stage_id}.{evidence.column}"
    if isinstance(evidence, StageEvidence):
        return f"stage {evidence.stage_id}"
    if isinstance(evidence, BranchEvidence):
        return f"branch {evidence.branch_id}"
    if isinstance(evidence, TermEvidence):
        return f"term {evidence.name}"
    raise ValueError(f"no words for evidence kind {evidence.kind!r}")


def _append_plain(tokens: list[SentenceToken], text: str) -> None:
    if text:
        tokens.append(SentenceToken(text=text))


def _find_worst_severity(challenges: list[Challenge], index: int) -> int | None:
    landed = [one.severity for one in challenges if one.grounding_index == index]
    return max(landed) if landed else None


def _read_phrase_order(grounding: list[Grounding]) -> list[int]:
    return sorted(range(len(grounding)), key=lambda index: grounding[index].start)


# ── what was raised ─────


def _build_open_challenges(text: str, review: ClaimReview | None) -> list[ChallengeCard]:
    return _build_cards(
        text, review, [one for one in _read_challenges(review) if one.severity > 0])


def _build_quiet_challenges(text: str, review: ClaimReview | None) -> list[ChallengeCard]:
    return _build_cards(
        text, review, [one for one in _read_challenges(review) if one.severity == 0])


def _build_cards(text: str, review: ClaimReview | None,
                 kept: list[Challenge]) -> list[ChallengeCard]:
    grounding = _read_grounding(review)
    order = _read_phrase_order(grounding)
    rank = {index: place for place, index in enumerate(order)}
    ordered = sorted(kept, key=lambda one: (
        -one.severity, _rank_the_phrase(rank, one.grounding_index, len(order))))
    return [_build_card(one, _read_phrase(text, grounding, one.grounding_index))
            for one in ordered]


def _rank_the_phrase(rank: dict[int, int], index: int | None, last: int) -> int:
    """A challenge on the whole sentence sorts after every one landing on a phrase."""
    return last if index is None else rank[index]


def _build_card(challenge: Challenge, phrase: str) -> ChallengeCard:
    return ChallengeCard(
        phrase=phrase,
        attacker=challenge.attacker,
        kind=challenge.kind,
        kind_words=KIND_WORDS[ChallengeKind(challenge.kind)],
        severity=challenge.severity,
        severity_words=SEVERITY_WORDS[challenge.severity],
        text=challenge.text,
        evidence=challenge.evidence,
        backing=challenge.backing,
        moves=challenge.moves,
        moves_words=MOVES_WORDS[Moves(challenge.moves)],
        cost_words=COST_WORDS[Cost(challenge.cost)],
        raised_by=challenge.raised_by,
    )


def _read_phrase(text: str, grounding: list[Grounding], index: int | None) -> str:
    """A challenge on no phrase is a challenge to the whole sentence."""
    if index is None:
        return text
    return text[grounding[index].start:grounding[index].end]


def _count_attackers(review: ClaimReview | None) -> list[AttackerCount]:
    raised = Counter(Attacker(one.attacker) for one in _read_challenges(review))
    return [
        AttackerCount(name=words.name, reads=words.reads, does=words.does,
                      count=raised[words.attacker])
        for words in ATTACKER_WORDS
    ]


# ── the state of the attack ─────


class _Attack(BaseModel):
    attack: str
    error: str | None = None
    session_id: ID | None = None


def _read_attack(claim: Claim, review: ClaimReview | None) -> _Attack:
    if review is not None:
        return _Attack(attack=ATTACK_DONE)
    if not isinstance(claim.citation, StageOutputCellCitation):
        return _Attack(attack=ATTACK_REFUSED)
    attacked = find_attack_sessions(claim.id)
    if not attacked:
        return _Attack(attack=ATTACK_NONE)
    return _read_session_attack(attacked[0])


def _read_session_attack(session: AgentSession) -> _Attack:
    if session.active_turn is not None:
        return _Attack(attack=ATTACK_RUNNING, session_id=session.id)
    failure = _find_generation_failure(session.id)
    if failure is None:
        return _Attack(attack=ATTACK_NONE, session_id=session.id)
    return _Attack(attack=ATTACK_FAILED, error=failure, session_id=session.id)


def _find_generation_failure(session_id: ID) -> str | None:
    return next(
        (text for text in SessionStore().read_last_reply_texts(session_id)
         if text.startswith(GENERATION_FAILURE_PREFIX)),
        None,
    )


# ── what the claim sits on ─────


def _read_run_row(project_id: ID, run_id: ID) -> RunIndexRow:
    for row in build_run_index_rows(project_id):
        if row.run_id == run_id:
            return row
    raise ClaimRefused([f"this project holds no run '{run_id}'"])


def _read_shape(project_id: ID, claim: Claim) -> ClaimShape:
    shape = load_claim_shape(project_id, claim.shape_id)
    if shape is None:
        raise ClaimRefused([f"this project holds no claim shape '{claim.shape_id}'"])
    return shape


def _read_grounding(review: ClaimReview | None) -> list[Grounding]:
    return list(review.grounding) if review is not None else []


def _read_challenges(review: ClaimReview | None) -> list[Challenge]:
    return list(review.challenges) if review is not None else []


def _build_rewrites(review: ClaimReview | None) -> list[RewriteCard]:
    if review is None:
        return []
    return [RewriteCard(text=one.text, why=one.why) for one in review.proposed_rewrites]
