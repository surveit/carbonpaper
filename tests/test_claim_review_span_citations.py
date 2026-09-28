"""A claim on a span, and challenges citing one: checked against the files the run read, then drawn."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from html import escape
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.files import compute_sha256, list_project_files, resolve_stored_path
from app.main import app
from app.models.citations import AddressedSourceSpanCitation, SourceSpanCitation
from app.models.claims import ClaimImportance, ClaimShapeInput, DataUniverseRequirement
from app.models.connectors import (
    CONNECTORS, SOURCE_COLUMNS, AcquiredBytes, ConnectorParams, ConnectorSpec,
)
from app.models.locators import PageCharRange
from app.models.packs import PACKS, PackSpec, register_pack
from app.models.records.claim_review import ChallengeKind, ClaimPart, DraftChallenge, Severity
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.reviewer.evidence import render_evidence_pool
from app.services import claim_review, claim_shapes, claims
from app.services import run as run_service
from app.services.errors import ClaimReviewRefused
from app.web.claim_review_view import build_claim_review_page
from conftest import reads_of
from pdf_fixture import write_text_pdf
from stage_seed import save_version, set_stages

PROJECT = "span_claims"
PAGE = "The letter says one thing and then another."
QUOTE = "one thing"
START = PAGE.index(QUOTE)
END = START + len(QUOTE)
TEXT = "The letter says one thing."
_ORIGIN = "https://example.org/letters/"
_KIND = "letter_folder"

_NARROWS_THE_PAGE = f'''
from app.models.spans import Span, narrow_span

def transform(row):
    said = narrow_span(Span.model_validate(row["page_span"]), {QUOTE!r})
    return dict(row, said=said.model_dump())
'''


class _FolderParams(ConnectorParams):
    folder: str


def _acquire_folder(params: _FolderParams) -> Iterator[AcquiredBytes]:
    for path in sorted(Path(params.folder).iterdir()):
        yield AcquiredBytes(filename=path.name, origin_url=_ORIGIN + path.name,
                            open_bytes=_opener(path), metadata={})


def _opener(path: Path) -> Callable[[], BinaryIO]:
    return lambda: path.open("rb")


@pytest.fixture
def letter_pack() -> Iterator[None]:
    saved_connectors, saved_packs = dict(CONNECTORS), dict(PACKS)
    register_pack(PackSpec(pack_id="letters", connectors=(ConnectorSpec(
        kind=_KIND, params_model=_FolderParams, metadata_columns=(),
        acquire=_acquire_folder),)))
    yield
    CONNECTORS.clear()
    CONNECTORS.update(saved_connectors)
    PACKS.clear()
    PACKS.update(saved_packs)


@pytest.fixture
def claim(projects_root: Path, letter_pack: None) -> Claim:
    folder = projects_root / PROJECT / "letters"
    folder.mkdir(parents=True)
    write_text_pdf(folder / "letter.pdf", [PAGE])
    [shape] = claim_shapes.write_claim_shapes(PROJECT, [ClaimShapeInput(
        label="What the letter says", universe=DataUniverseRequirement.open,
        importance=ClaimImportance.primary)])
    set_stages(PROJECT, _stage_specs(folder, shape.id))
    save_version(PROJECT, message="fixture")
    run_id = str(run_service.execute(PROJECT)["run_id"])
    return claims.submit_claim(PROJECT, run_id, "what-it-says", {}, TEXT)


def _stage_specs(folder: Path, shape_id: str) -> list[dict[str, Any]]:
    naming_a_file = [column.model_dump(mode="json", exclude_defaults=True)
                     for column in SOURCE_COLUMNS[:2]]
    page_span = {"name": "page_span", "type": "span", "nullable": False}
    return [
        {"id": "letters", "description": "Read the letters", "type": "input_data",
         "connector": {"kind": _KIND, "params": {"folder": str(folder)}},
         "signature": {"form": "replaces", "produces": [
             column.model_dump(mode="json", exclude_defaults=True)
             for column in SOURCE_COLUMNS]}},
        {"id": "pages", "description": "Read each letter a page at a time",
         "type": "read_pages", "inputs": [{"id": "letters"}], "row_type_id": "letter_page",
         "read_pages": {"carry": []},
         "signature": {"form": "replaces", "reads": reads_of("letters", naming_a_file),
                       "produces": [*naming_a_file,
                                    {"name": "page", "type": "int", "nullable": False},
                                    {"name": "page_text", "type": "str", "nullable": False},
                                    page_span]}},
        {"id": "quoted", "description": "Quotes what the letter says", "type": "python_row_function",
         "inputs": [{"id": "pages"}],
         "function": {"kind": "inline", "code": _NARROWS_THE_PAGE},
         "signature": {"form": "extends", "reads": reads_of("pages", [page_span]),
                       "adds": [{"name": "said", "type": "span", "nullable": False}]},
         "workflow_outputs": [{"kind": "figure", "slug": "what-it-says",
                               "label": "What the letter says", "column": "said",
                               "shape_id": shape_id}]},
    ]


def _cite(claim: Claim, **moved: Any) -> SourceSpanCitation:
    assert isinstance(claim.citation, SourceSpanCitation)
    return claim.citation.model_copy(update=moved)


def _challenge(*citations: SourceSpanCitation) -> DraftChallenge:
    return DraftChallenge(
        kind=ChallengeKind.meaning, claim_part=ClaimPart(phrase="one thing"),
        text="The letter goes on to say another.",
        justification="The page text after the quote is «and then another.»",
        citations=list(citations), severity=Severity.low)


def _store(claim: Claim, *citations: SourceSpanCitation) -> Any:
    return claim_review.store_claim_review(
        PROJECT, claim.id, challenges=[_challenge(*citations)], session_id="session-review")


# ── what the run publishes ─────


def test_a_figure_over_a_span_cell_publishes_its_quote_and_address(claim):
    [letter] = list_project_files(PROJECT)
    [output] = [one for one in WorkflowOutput.list() if one.slug == "what-it-says"]

    assert output.citation == SourceSpanCitation(
        run_id=claim.citation.run_id, stage_id="quoted", row_ordinal=0, column="said",
        source_id=letter.id, source_sha256=letter.sha256,
        locator=PageCharRange(page=1, start=START, end=END), quote=QUOTE)
    assert claim.citation == output.citation


# ── what the store accepts ─────


def test_a_challenge_citing_a_span_that_holds_is_stored(claim):
    review = _store(claim, _cite(claim))

    [cited] = review.challenges[0].citations
    assert isinstance(cited, AddressedSourceSpanCitation) and cited.project_id == PROJECT
    assert cited.quote == QUOTE


def test_a_quote_not_at_its_address_is_refused_naming_what_the_page_holds_there(claim):
    moved = PageCharRange(page=1, start=START + 1, end=END + 1)

    with pytest.raises(ClaimReviewRefused) as refused:
        _store(claim, _cite(claim, locator=moved))

    assert refused.value.refusals == [
        "challenge 0 (meaning): source_span citation quote not at page 1 of letter.pdf: "
        f"characters {START + 1}–{END + 1} hold {PAGE[START + 1:END + 1]!r}, not {QUOTE!r}"]


def test_a_span_citation_whose_range_is_not_its_quotes_length_does_not_parse(claim):
    short = PageCharRange(page=1, start=START, end=START + 3)

    with pytest.raises(ValidationError, match="covers 3 characters, but the quote has 9"):
        SourceSpanCitation.model_validate({**claim.citation.model_dump(), "locator": short})


def test_a_span_on_a_file_the_run_did_not_read_is_refused(claim):
    unread = "0" * 64

    with pytest.raises(ClaimReviewRefused, match=f"this run read no file with sha256 {unread}"):
        _store(claim, _cite(claim, source_sha256=unread))


def test_a_span_that_holds_but_is_not_in_the_cited_cell_is_refused(claim):
    elsewhere = _cite(claim, locator=PageCharRange(page=1, start=0, end=3), quote="The")

    with pytest.raises(ClaimReviewRefused, match="quotes 'The' on page 1, which that cell"):
        _store(claim, elsewhere)


# ── what a reviewer reads ─────


def test_the_pool_carries_the_quote_the_page_text_around_it_and_the_citation(claim):
    pool = render_evidence_pool(claim_review.build_evidence_bundle(PROJECT, claim.id))

    assert f"quote: «{QUOTE}» on page 1" in pool
    assert "page text before it: «The letter says»" in pool
    assert "page text after it: «and then another.»" in pool
    [cite_line] = [line for line in pool.splitlines() if line.startswith("cite it as: ")]
    copied = SourceSpanCitation.model_validate_json(cite_line.removeprefix("cite it as: "))
    assert _store(claim, copied).challenges[0].citations[0].quote == QUOTE


def test_a_claim_whose_file_changed_since_the_run_is_refused_a_review(claim):
    [letter] = list_project_files(PROJECT)
    stored = resolve_stored_path(letter)
    stored.write_bytes(stored.read_bytes() + b"\n")

    with pytest.raises(ClaimReviewRefused, match=f"now hashes to {compute_sha256(stored)}"):
        claim_review.build_evidence_bundle(PROJECT, claim.id)


# ── the claim page ─────


def test_the_claim_page_shows_the_quote_its_page_and_links_the_source_page(claim):
    _store(claim, _cite(claim))
    [letter] = list_project_files(PROJECT)
    source_page = f"/project/{PROJECT}/files/{letter.id}?page=1&start={START}&end={END}"

    with TestClient(app) as client:
        html = client.get(f"/project/{PROJECT}/claims/{claim.id}").text

    assert f'<q>{QUOTE}</q>\n  <a href="{escape(source_page)}">page 1</a>' in html
    page = build_claim_review_page(PROJECT, claim.id)
    [cited] = page.challenges[0].citations
    assert (cited.kind_words, cited.trail, cited.quote, cited.href) == (
        "quote", ["quoted", "said", "row 0", "page 1"], QUOTE, source_page)
    assert f'<q class="cite-quote">{QUOTE}</q>' in html
