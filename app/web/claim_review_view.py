"""What the claim page draws, and the state of the review behind it."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.ids import ID
from app.core.figure_text import render_figure
from app.models.citations import (
    AddressedChallengeCitation,
    AddressedStageOutputCellCitation,
    PublishedCitation,
    StageOutputCellCitation,
)
from app.models.records.claim_review import (
    SEVERITY_WORDS,
    Challenge,
    ClaimPart,
    ClaimReview,
    Severity,
)
from app.models.records.claims import Claim, ClaimShape
from app.runtime.citations import build_row_trace_url
from app.services import claims as claims_service
from app.services.claim_review import load_claim_review
from app.services.claim_review_run import read_review_state
from app.services.claim_shapes import load_claim_shape
from app.services.errors import ClaimRefused
from app.models.run_manifest import RunKind
from app.services.run import read_run_manifest
from app.web.citation_links import render_source_url
from app.web.claims_view import describe_what_blocks_the_run
from app.web.panel_links import AppPanelLinks
from app.web.run_index import RunIndexRow, find_run_row



class LegendRow(BaseModel):
    severity: int
    name: str
    words: str


class SentenceToken(BaseModel):
    """One run of the sentence: `severity` is the worst of every challenge covering it."""

    text: str
    severity: int | None = None
    anchor: str | None = None


class CitationLink(BaseModel):
    """Read as a trail, so where a citation sits is the same shape as where the reader is."""

    kind_words: str
    trail: list[str]
    value: str
    href: str


class ChallengeCard(BaseModel):
    anchor: str
    phrase: str
    severity: int
    severity_name: str
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
    challenges: list[ChallengeCard]
    legend: list[LegendRow]


def build_claim_review_page(project_id: ID, claim_id: ID) -> ClaimReviewPage:
    claim = claims_service.load_claim(project_id, claim_id)
    review = load_claim_review(claim_id)
    run = _read_run_row(project_id, claim.citation.run_id)
    shape = _read_shape(project_id, claim)
    return _build_page(project_id, claim, shape, run, review)


# ── the page ─────


def _build_page(project_id: ID, claim: Claim, shape: ClaimShape, run: RunIndexRow,
                review: ClaimReview | None) -> ClaimReviewPage:
    running = read_review_state(claim, review)
    cards = _build_cards(_read_challenges(review))
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
        challenges=cards,
        tokens=_build_tokens(claim.text, cards),
        legend=_build_legend(),
    )


def _build_legend() -> list[LegendRow]:
    return [LegendRow(severity=weight, name=weight.name, words=words)
            for weight, words in sorted(SEVERITY_WORDS.items(), reverse=True)]


def _read_whether_the_run_read_everything(project_id: ID, claim: Claim) -> bool:
    manifest = read_run_manifest(project_id, claim.citation.run_id, RunKind.production)
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


def _build_tokens(text: str, cards: list[ChallengeCard]) -> list[SentenceToken]:
    """Two challenges may land on overlapping phrases, so the sentence is cut at every edge."""
    landed = [(span, card) for span, card in
              ((_find_span_of(text, card.phrase), card) for card in cards)
              if span is not None]
    return [_build_token(text[start:end], _worst_over(landed, start, end))
            for start, end in _build_runs_between_edges(text, [span for span, _ in landed])]


def _build_token(text: str, worst: ChallengeCard | None) -> SentenceToken:
    if worst is None:
        return SentenceToken(text=text)
    return SentenceToken(text=text, severity=worst.severity, anchor=worst.anchor)


def _find_span_of(text: str, phrase: str) -> tuple[int, int] | None:
    return _find_span(text, ClaimPart(phrase=phrase)) if phrase else None


def _find_span(text: str, part: ClaimPart | None) -> tuple[int, int] | None:
    if part is None:
        return None
    start = -1
    for _ in range(part.occurrence):
        start = text.find(part.phrase, start + 1)
        if start == -1:
            return None
    return start, start + len(part.phrase)


def _build_runs_between_edges(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    edges = sorted({0, len(text), *(edge for span in spans for edge in span)})
    return [(start, end) for start, end in zip(edges, edges[1:]) if end > start]


def _worst_over(landed: list[tuple[tuple[int, int], ChallengeCard]],
                start: int, end: int) -> ChallengeCard | None:
    covering = [card for (span_start, span_end), card in landed
                if span_start <= start and end <= span_end]
    return max(covering, key=lambda card: card.severity) if covering else None


# ── what was raised ─────


def _build_cards(challenges: list[Challenge]) -> list[ChallengeCard]:
    ranked = sorted(enumerate(challenges), key=lambda pair: -pair[1].severity)
    return [_build_card(f"challenge-{index}", one) for index, one in ranked]


def _build_card(anchor: str, challenge: Challenge) -> ChallengeCard:
    weight = Severity(challenge.severity)
    return ChallengeCard(
        anchor=anchor,
        phrase=challenge.claim_part.phrase if challenge.claim_part else "",
        severity=weight,
        severity_name=weight.name,
        severity_words=SEVERITY_WORDS[weight],
        text=challenge.text,
        justification=challenge.justification,
        citations=[_build_citation_link(one) for one in challenge.citations],
    )


CITATION_KIND_WORDS: dict[str, str] = {
    "stage_output_cell": "cell",
    "stage_output_column": "column",
    "stage": "stage",
    "term": "term",
}


def _build_citation_link(citation: AddressedChallengeCitation) -> CitationLink:
    return CitationLink(kind_words=CITATION_KIND_WORDS[citation.kind],
                        trail=_build_trail(citation),
                        value=_read_cited_figure(citation),
                        href=render_source_url(citation))


def _build_trail(citation: AddressedChallengeCitation) -> list[str]:
    if citation.kind == "stage_output_cell":
        return [citation.stage_id, citation.column, f"row {citation.row_ordinal}"]
    if citation.kind == "stage_output_column":
        return [citation.stage_id, citation.column]
    if citation.kind == "stage":
        return [citation.stage_id]
    return [citation.name]


def _read_cited_figure(citation: AddressedChallengeCitation) -> str:
    """Only a cell citation names a value; the other kinds point at no single one."""
    if isinstance(citation, AddressedStageOutputCellCitation):
        return render_figure(citation.value)
    return ""


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
