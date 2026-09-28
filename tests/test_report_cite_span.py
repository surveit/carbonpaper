from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import CitationMismatch
from app.core.files import compute_sha256
from app.core.frames import table_from_rows
from app.core.text_sources import read_page_text
from app.models import parse_stage
from app.models.citations import CitedValue
from app.models.locators import PageCharRange
from app.models.spans import Span, narrow_span
from app.runtime.citations import CitationProvider
from app.runtime.context import RunContext
from app.runtime.spans import SourceTextCache
from app.runtime.stages.report import handle_report
from conftest import place_stage
from pdf_fixture import write_text_pdf

FIRST_PAGE = "The first page says one thing."
SECOND_PAGE = "The second page says another."


@pytest.fixture
def two_pages(tmp_path: Path) -> Path:
    return write_text_pdf(tmp_path / "two_pages.pdf", [FIRST_PAGE, SECOND_PAGE])


def _second_page(path: Path) -> Span:
    text = read_page_text(path, 2)
    return Span(source_id="stored_pdf", source_sha256=compute_sha256(path),
                locator=PageCharRange(page=2, start=0, end=len(text)), quote=text)


def _provider(path: Path, spans: list[Span]) -> CitationProvider:
    rows = [{"basis": span.model_dump(), "supporting": [span.model_dump()]} for span in spans]
    return CitationProvider(
        project="docket", run_id="R1", tables={"extract": table_from_rows(rows)},
        sources={compute_sha256(path): path}, texts=SourceTextCache(),
    )


def test_a_cited_span_links_to_its_page_with_the_quote_s_range(two_pages: Path) -> None:
    span = narrow_span(_second_page(two_pages), "says another")
    url = _provider(two_pages, [span]).cite_span("extract", 0, "basis", span, label="the reply")
    assert url == "/project/docket/files/stored_pdf?page=2&start=16&end=28"


def test_a_cited_span_is_recorded_as_its_quote_in_the_cell_that_holds_it(two_pages: Path) -> None:
    span = narrow_span(_second_page(two_pages), "says another")
    provider = _provider(two_pages, [span])
    provider.cite_span("extract", 0, "supporting", span, label="the reply")
    assert provider.citations == [CitedValue(
        stage_id="extract", row_ordinal=0, column="supporting", label="the reply",
        value="says another",
    )]


def test_a_span_the_cell_does_not_hold_is_refused(two_pages: Path) -> None:
    page = _second_page(two_pages)
    provider = _provider(two_pages, [page])
    narrower = narrow_span(page, "another")
    with pytest.raises(CitationMismatch, match="'the reply' cites extract.basis row 0 for the span"):
        provider.cite_span("extract", 0, "basis", narrower, label="the reply")
    assert provider.citations == []


def test_a_held_span_whose_quote_is_not_at_its_address_is_refused(two_pages: Path) -> None:
    page = _second_page(two_pages)
    first_page = PageCharRange(page=1, start=0, end=len(page.quote))
    misplaced = page.model_copy(update={"locator": first_page})
    provider = _provider(two_pages, [misplaced])
    refusal = "'the reply' cites a span its file refuses: quote not at page 1"
    with pytest.raises(CitationMismatch, match=refusal):
        provider.cite_span("extract", 0, "basis", misplaced, label="the reply")
    assert provider.citations == []


# ── through the handler ───────────────────────────────────────────────────────

_CITES_A_QUOTE = """
import pathlib
from app.models.spans import Span

def transform(extract, output_dir, citation_provider):
    span = Span.model_validate(extract["basis"].iloc[0])
    url = citation_provider.cite_span("extract", 0, "basis", span, label="the reply")
    path = pathlib.Path(output_dir) / "index.html"
    path.write_text("<a href='" + url + "'>" + span.quote + "</a>", encoding="utf-8")
    return pd.DataFrame({"path": [str(path)]})
"""


def test_a_report_cites_a_span_against_the_files_its_run_read(
    two_pages: Path, tmp_path: Path
) -> None:
    span = narrow_span(_second_page(two_pages), "says another")
    ctx = RunContext.for_workflow_run(
        tmp_path / "run", "docket", "R1", bound_sources={compute_sha256(two_pages): two_pages})
    stage = parse_stage({
        "id": "publish_reply", "type": "report", "description": "Publish the reply",
        "inputs": [{"id": "extract"}],
        "report": {"format": "html_report", "destination": "build/"},
        "signature": {"form": "replaces"},
        "function": {"kind": "inline", "code": "import pandas as pd\n" + _CITES_A_QUOTE},
    })
    extract = table_from_rows([{"basis": span.model_dump()}])
    handle_report(place_stage(stage), {"extract": extract}, ctx)
    html = (tmp_path / "run" / "artifacts" / "build" / "index.html").read_text(encoding="utf-8")
    assert html == (
        "<a href='/project/docket/files/stored_pdf?page=2&start=16&end=28'>says another</a>")
