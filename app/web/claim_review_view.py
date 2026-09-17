"""What the claim page draws, and the state of the review behind it."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.agent.store import AgentSession, SessionStore
from app.core.ids import ID
from app.models.citations import (
    AddressedChallengeCitation,
    PublishedCitation,
    StageOutputCellCitation,
)
from app.models.records.claim_review import (
    SEVERITY_WORDS,
    Severity,
    Challenge,
    ChallengeKind,
    ClaimPart,
    ClaimReview,
)
from app.models.records.claims import Claim, ClaimShape
from app.runtime.citations import build_row_trace_url
from app.services import claims as claims_service
from app.services.claim_review import load_claim_review
from app.services.claim_review_run import find_review_sessions
from app.services.claim_shapes import load_claim_shape
from app.services.errors import ClaimRefused
from app.services.generation import GENERATION_FAILURE_PREFIX
from app.services.run import read_run_manifest
from app.web.citation_links import render_source_url
from app.web.claims_view import describe_what_blocks_the_run
from app.web.panel_links import AppPanelLinks
from app.web.run_index import RunIndexRow, find_run_row

REVIEW_NONE = "none"
REVIEW_RUNNING = "running"
REVIEW_FAILED = "failed"
REVIEW_DONE = "done"
REVIEW_REFUSED = "refused"

KIND_WORDS: dict[ChallengeKind, str] = {
    ChallengeKind.data: "data",
    ChallengeKind.choice: "a choice made",
    ChallengeKind.omission: "a decision never made",
    ChallengeKind.coverage: "coverage",
    ChallengeKind.meaning: "what it means",
    ChallengeKind.gap: "not examined",
}


class SentenceToken(BaseModel):
    """One run of the sentence: `severity` is the worst of every challenge covering it."""

    text: str
    severity: int | None = None


class CitationLink(BaseModel):
    words: str
    href: str


class ChallengeCard(BaseModel):
    phrase: str
    kind: str
    kind_words: str
    severity: int
    severity_words: str
    text: str
    justification: str
    citations: list[CitationLink]


class OutputRow(BaseModel):
    slug: str
    label: str
    value: str
    href: str
    cited: bool


class ClaimReviewPage(BaseModel):
    claim_id: ID
    run_id: ID
    status: str
    text: str
    value: str
    value_href: str
    shape_label: str
    universe: str
    run_read_everything: bool
    blocked: str
    outputs: list[OutputRow]
    review: str
    review_error: str | None
    review_session_id: ID | None
    tokens: list[SentenceToken]
    open_challenges: list[ChallengeCard]
    quiet_challenges: list[ChallengeCard]


def build_claim_review_page(project_id: ID, claim_id: ID) -> ClaimReviewPage:
    claim = claims_service.load_claim(project_id, claim_id)
    review = load_claim_review(claim_id)
    run = _read_run_row(project_id, claim.citation.run_id)
    shape = _read_shape(project_id, claim)
    return _build_page(project_id, claim, shape, run, review)


# ── the page ─────


def _build_page(project_id: ID, claim: Claim, shape: ClaimShape, run: RunIndexRow,
                review: ClaimReview | None) -> ClaimReviewPage:
    running = _read_review_state(claim, review)
    challenges = _read_challenges(review)
    return ClaimReviewPage(
        claim_id=claim.id,
        run_id=claim.citation.run_id,
        status=claim.status,
        text=claim.text,
        value=_read_cited_value(claim),
        value_href=_build_citation_href(project_id, claim.citation),
        shape_label=shape.label,
        universe=shape.universe,
        run_read_everything=_read_whether_the_run_read_everything(project_id, claim),
        blocked=describe_what_blocks_the_run(run),
        outputs=_build_outputs(project_id, claim),
        review=running.review,
        review_error=running.error,
        review_session_id=running.session_id,
        tokens=_build_tokens(claim.text, challenges),
        open_challenges=_build_cards([one for one in challenges if one.severity > 0]),
        quiet_challenges=_build_cards([one for one in challenges if one.severity == 0]),
    )


def _read_whether_the_run_read_everything(project_id: ID, claim: Claim) -> bool:
    manifest = read_run_manifest(project_id, claim.citation.run_id)
    return claims_service.read_whether_the_run_read_everything(manifest)


def _read_cited_value(claim: Claim) -> str:
    cited = claim.citation
    return str(cited.value) if isinstance(cited, StageOutputCellCitation) else ""


def _build_outputs(project_id: ID, claim: Claim) -> list[OutputRow]:
    cited = claims_service.find_output_of_claim(claim)
    return [
        OutputRow(slug=output.slug, label=output.label,
                  value=_read_output_value(output.citation),
                  href=_build_citation_href(project_id, output.citation),
                  cited=output.slug == cited.slug)
        for output in claims_service.read_workflow_run_outputs(claim.citation.run_id)
    ]


def _read_output_value(citation: PublishedCitation) -> str:
    return str(citation.value) if isinstance(citation, StageOutputCellCitation) else ""


def _build_citation_href(project_id: ID, citation: PublishedCitation) -> str:
    """A table output names no row, so its rows page is where its rectangle is read."""
    cell = citation if isinstance(citation, StageOutputCellCitation) else None
    if cell is None:
        return AppPanelLinks(project_id, citation.run_id).stage_rows(citation.stage_id)
    return build_row_trace_url(project_id, citation.run_id, citation.stage_id,
                               cell.row_ordinal, column=cell.column)


# ── the sentence ─────


def _build_tokens(text: str, challenges: list[Challenge]) -> list[SentenceToken]:
    """Two challenges may land on overlapping phrases, so the sentence is cut at every edge."""
    spans = [(span, one.severity) for one, span in
             ((one, _find_span(text, one.claim_part)) for one in challenges)
             if span is not None]
    return [SentenceToken(text=text[start:end], severity=_worst_over(spans, start, end))
            for start, end in _cut_at_every_edge(text, [span for span, _ in spans])]


def _find_span(text: str, part: ClaimPart | None) -> tuple[int, int] | None:
    if part is None:
        return None
    start = -1
    for _ in range(part.occurrence):
        start = text.find(part.phrase, start + 1)
        if start == -1:
            return None
    return start, start + len(part.phrase)


def _cut_at_every_edge(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    edges = sorted({0, len(text), *(edge for span in spans for edge in span)})
    return [(start, end) for start, end in zip(edges, edges[1:]) if end > start]


def _worst_over(spans: list[tuple[tuple[int, int], Severity]],
                start: int, end: int) -> int | None:
    covering = [severity for (span_start, span_end), severity in spans
                if span_start <= start and end <= span_end]
    return max(covering) if covering else None


# ── what was raised ─────


def _build_cards(challenges: list[Challenge]) -> list[ChallengeCard]:
    return [_build_card(one) for one in
            sorted(challenges, key=lambda one: -one.severity)]


def _build_card(challenge: Challenge) -> ChallengeCard:
    return ChallengeCard(
        phrase=challenge.claim_part.phrase if challenge.claim_part else "",
        kind=challenge.kind,
        kind_words=KIND_WORDS[ChallengeKind(challenge.kind)],
        severity=challenge.severity,
        severity_words=SEVERITY_WORDS[challenge.severity],
        text=challenge.text,
        justification=challenge.justification,
        citations=[_build_citation_link(one) for one in challenge.citations],
    )


def _build_citation_link(citation: AddressedChallengeCitation) -> CitationLink:
    return CitationLink(words=_describe_citation(citation),
                        href=render_source_url(citation))


def _describe_citation(citation: AddressedChallengeCitation) -> str:
    if citation.kind == "stage_output_cell":
        return f"{citation.stage_id}.{citation.column} row {citation.row_ordinal}"
    if citation.kind == "stage_output_column":
        return f"{citation.stage_id}.{citation.column}"
    if citation.kind == "stage":
        return f"stage {citation.stage_id}"
    return f"term {citation.name}"


# ── the review behind it ─────


class _ReviewState(BaseModel):
    review: str
    error: str | None = None
    session_id: ID | None = None


def _read_review_state(claim: Claim, review: ClaimReview | None) -> _ReviewState:
    if review is not None:
        return _ReviewState(review=REVIEW_DONE, session_id=review.session_id)
    if not isinstance(claim.citation, StageOutputCellCitation):
        return _ReviewState(review=REVIEW_REFUSED)
    reviewed = find_review_sessions(claim.id)
    if not reviewed:
        return _ReviewState(review=REVIEW_NONE)
    return _read_session_state(reviewed[0])


def _read_session_state(session: AgentSession) -> _ReviewState:
    if session.active_turn is not None:
        return _ReviewState(review=REVIEW_RUNNING, session_id=session.id)
    failure = _find_generation_failure(session.id)
    if failure is None:
        return _ReviewState(review=REVIEW_NONE, session_id=session.id)
    return _ReviewState(review=REVIEW_FAILED, error=failure, session_id=session.id)


def _find_generation_failure(session_id: ID) -> str | None:
    return next(
        (text for text in SessionStore().read_last_reply_texts(session_id)
         if text.startswith(GENERATION_FAILURE_PREFIX)),
        None,
    )


# ── what the claim sits on ─────


def _read_run_row(project_id: ID, run_id: ID) -> RunIndexRow:
    row = find_run_row(project_id, run_id)
    if row is None:
        raise ClaimRefused([f"this project holds no run '{run_id}'"])
    return row


def _read_shape(project_id: ID, claim: Claim) -> ClaimShape:
    shape = load_claim_shape(project_id, claim.shape_id)
    if shape is None:
        raise ClaimRefused([f"this project holds no claim shape '{claim.shape_id}'"])
    return shape


def _read_challenges(review: ClaimReview | None) -> list[Challenge]:
    return list(review.challenges) if review is not None else []
