"""An llm_transform asks for a quote alone and mints the span from the column it quotes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import (
    contribution_of,
    make_run_context,
    place_stage,
    reads_of,
    script_judgment,
    source_stage,
)
from fastapi.testclient import TestClient
from stage_seed import set_parsed_stages

from app.core.files import compute_sha256, resolve_stored_path, save_upload
from app.core.frames import list_table_rows, table_from_rows
from app.core.text_sources import read_page_text
from app.main import app
from app.models import Stage, Workflow, parse_stage
from app.models.locators import PageCharRange
from app.models.run_manifest import InputBinding
from app.models.stage import StageType
from app.models.spans import Span, narrow_span
from app.runtime.context import RunContext
from app.runtime.llm import render_prompt
from app.runtime.runner import prepare_run
from app.runtime.spans import SourceTextCache, find_span_issues
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from app.runtime.stages.execution import StageOutput
from app.runtime.stages.read_pages import handle_read_pages

# https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.58.0.pdf
ECF_58_PAGE_1 = Path(__file__).parent / "fixtures" / "us_v_boeing_ecf58_page1.pdf"
# It repeats its case header and ends lines with " \n", so a model's copy differs in spacing.
PAGE_TEXT = read_page_text(ECF_58_PAGE_1, 1)
STORED_FILE_ID = "stored_ecf_58"
TEMPLATE = "Quote the court's name from this page:\n{page}"
CASE_HEADER = "Case 4:21-cr-00005-O Document 58"


def _page_span() -> Span:
    return Span(
        source_id=STORED_FILE_ID, source_sha256=compute_sha256(ECF_58_PAGE_1),
        locator=PageCharRange(page=1, start=0, end=len(PAGE_TEXT)), quote=PAGE_TEXT,
    )


def _bound_sources(file_id: str = STORED_FILE_ID) -> dict[str, InputBinding]:
    sha256 = compute_sha256(ECF_58_PAGE_1)
    return {sha256: InputBinding(stage_id="load", path=str(ECF_58_PAGE_1),
                                 filename=ECF_58_PAGE_1.name, sha256=sha256, file_id=file_id)}


def _quoting_stage(
    *, quoted_type: str = "span", batch_size: int = 1, max_retries: int = 1
) -> Stage:
    return parse_stage({
        "id": "extract", "description": "Quote the court", "type": "llm_transform",
        "inputs": [{"id": "pages"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "pages", "columns": [
                {"name": "page", "type": quoted_type, "nullable": True}]}],
            "adds": [{"name": "basis", "type": "span", "nullable": True, "quoted_from": "page"}]},
        "llm": {"prompt_data_template": TEMPLATE, "batch_size": batch_size,
                "max_retries": max_retries},
    })


def _run(stage: Stage, cells: list[Any], ctx: RunContext | None = None) -> StageOutput:
    rows = [{"page": cell} for cell in cells]
    context = ctx or make_run_context().model_copy(update={"bound_sources": _bound_sources()})
    output = HANDLERS[StageType.llm_transform].execute(
        place_stage(stage), {"pages": table_from_rows(rows)}, context)
    assert output is not None
    return output


def _script_replies(monkeypatch, *quotes: dict[str, str]) -> list[dict[str, Any]]:
    """Answers each call with the next reply; returns what each call was shown."""
    calls: list[dict[str, Any]] = []

    def answer(stage_id, llm_config, row, *, reply_model, usage_out, correction):
        calls.append({"prompt": render_prompt(llm_config.prompt_data_template, row),
                      "correction": correction})
        return script_judgment({"basis": quotes[len(calls) - 1]})

    monkeypatch.setattr(lt, "call_llm", answer)
    return calls


def _minted(output: StageOutput) -> list[Span | None]:
    return [None if row["basis"] is None else Span.model_validate(row["basis"])
            for row in list_table_rows(output.table)]


def _verify(output: StageOutput, stage: Stage) -> None:
    schema = place_stage(stage).output_schema
    assert schema is not None
    assert find_span_issues(output.table, schema, _bound_sources(), SourceTextCache()) == []


def test_an_exact_quote_is_minted_at_its_address_on_the_page_and_verifies(monkeypatch):
    calls = _script_replies(monkeypatch, {"quote": "FORT WORTH DIVISION"})
    stage = _quoting_stage()

    output = _run(stage, [_page_span().model_dump()])

    (span,) = _minted(output)
    start = PAGE_TEXT.index("FORT WORTH DIVISION")
    assert span == Span(source_id=STORED_FILE_ID, source_sha256=compute_sha256(ECF_58_PAGE_1),
                        locator=PageCharRange(page=1, start=start, end=start + 19),
                        quote="FORT WORTH DIVISION")
    _verify(output, stage)
    assert calls[0]["prompt"] == TEMPLATE.format(page=PAGE_TEXT)


def test_a_quote_whose_whitespace_differs_is_minted_as_the_page_s_own_characters(monkeypatch):
    _script_replies(monkeypatch, {
        "quote": "IN THE UNITED STATES DISTRICT COURT FOR THE NORTHERN DISTRICT OF TEXAS"})
    stage = _quoting_stage()

    output = _run(stage, [_page_span().model_dump()])

    (span,) = _minted(output)
    assert span is not None and isinstance(span.locator, PageCharRange)
    assert span.quote == "IN THE UNITED STATES DISTRICT COURT \nFOR THE NORTHERN DISTRICT OF TEXAS"
    assert PAGE_TEXT[span.locator.start:span.locator.end] == span.quote
    _verify(output, stage)


def test_a_quote_not_on_the_page_is_re_asked_then_fails_the_row_naming_file_page_and_quote(
    monkeypatch,
):
    calls = _script_replies(monkeypatch, {"quote": "the plea was rejected"},
                            {"quote": "the plea was rejected"})

    output = _run(_quoting_stage(max_retries=1), [_page_span().model_dump()])

    refusal = "quote not found on page 1 of us_v_boeing_ecf58_page1.pdf: 'the plea was rejected'"
    assert [call["correction"] for call in calls] == [None, refusal]
    (error,) = contribution_of(output).row_errors
    assert error["message"] == f"reply rejected after 2 attempt(s): {refusal}"
    assert _minted(output) == [None]


def test_a_second_reply_that_quotes_the_page_is_kept(monkeypatch):
    calls = _script_replies(monkeypatch, {"quote": "the plea was rejected"},
                            {"quote": "FORT WORTH DIVISION"})

    output = _run(_quoting_stage(max_retries=1), [_page_span().model_dump()])

    assert len(calls) == 2
    assert not contribution_of(output).row_errors
    assert [span.quote for span in _minted(output) if span] == ["FORT WORTH DIVISION"]


def test_a_quote_found_twice_is_re_asked_and_a_prefix_picks_one(monkeypatch):
    calls = _script_replies(monkeypatch, {"quote": CASE_HEADER},
                            {"quote": CASE_HEADER, "prefix": "PageID 536"})
    stage = _quoting_stage(max_retries=1)

    output = _run(stage, [_page_span().model_dump()])

    assert calls[1]["correction"] == (
        "quote appears 2 times on page 1 of us_v_boeing_ecf58_page1.pdf; give a prefix or "
        f"suffix that tells them apart: {CASE_HEADER!r}")
    (span,) = _minted(output)
    assert span is not None and isinstance(span.locator, PageCharRange)
    assert span.locator.start == PAGE_TEXT.rindex("Case 4:21-cr-00005-O")
    assert (span.prefix, span.quote) == ("PageID 536", "Case 4:21-cr-00005-O   Document 58")
    _verify(output, stage)


def test_a_run_that_bound_no_files_names_the_file_by_its_stored_id(monkeypatch):
    _script_replies(monkeypatch, {"quote": "the plea was rejected"})

    output = _run(_quoting_stage(max_retries=0), [_page_span().model_dump()], make_run_context())

    (error,) = contribution_of(output).row_errors
    assert "quote not found on page 1 of stored file stored_ecf_58" in error["message"]


def test_a_file_the_run_read_as_another_stored_file_fails_the_row_without_a_re_ask(monkeypatch):
    calls = _script_replies(monkeypatch, {"quote": "FORT WORTH DIVISION"})
    ctx = make_run_context().model_copy(
        update={"bound_sources": _bound_sources(file_id="another_upload")})

    output = _run(_quoting_stage(max_retries=1), [_page_span().model_dump()], ctx)

    assert len(calls) == 1
    (error,) = contribution_of(output).row_errors
    assert "as stored file 'another_upload'" in error["message"]


def test_a_page_read_pages_read_is_quoted_and_the_span_verifies_against_the_stored_file(
    monkeypatch, tmp_path,
):
    with ECF_58_PAGE_1.open("rb") as stream:
        record = save_upload(ECF_58_PAGE_1.name, stream, project_id="boeing_docket")
    filing = [{"name": "source_id", "type": "str", "nullable": False},
              {"name": "source_sha256", "type": "str", "nullable": False}]
    filings = {**source_stage("filings", filing), "connector": {
        "kind": "file", "params": {"paths": [str(resolve_stored_path(record))]}}}
    read_pages = parse_stage({
        "id": "pages", "description": "Read each filing a page at a time", "type": "read_pages",
        "inputs": [{"id": "filings"}], "row_type_id": "filing_page", "read_pages": {"carry": []},
        "signature": {"form": "replaces", "reads": reads_of("filings", filing), "produces": [
            *filing, {"name": "page", "type": "int", "nullable": False},
            {"name": "page_text", "type": "str", "nullable": False},
            {"name": "page_span", "type": "span", "nullable": False}]}})
    ctx: RunContext = prepare_run(tmp_path / "runs", "boeing_docket", Workflow(
        stages=[parse_stage(filings), read_pages]), "v1")["ctx"]
    pages = handle_read_pages(place_stage(read_pages), {"filings": table_from_rows(
        [{"source_id": record.id, "source_sha256": record.sha256}])}, ctx)
    _script_replies(monkeypatch, {"quote": "FOR THE NORTHERN DISTRICT OF TEXAS FORT WORTH"})
    page_spans = [row["page_span"] for row in list_table_rows(pages.table)]

    output = _run(_quoting_stage(), page_spans, ctx)

    (span,) = _minted(output)
    assert span is not None and span.source_id == record.id
    assert span.quote == "FOR THE NORTHERN DISTRICT OF TEXAS \nFORT WORTH"
    schema = place_stage(_quoting_stage()).output_schema
    assert schema is not None
    assert find_span_issues(output.table, schema, ctx.bound_sources, SourceTextCache()) == []


# ── a list[span] column: the quote must be one of the spans shown ──
def _listed_spans() -> list[dict[str, Any]]:
    page = _page_span()
    return [narrow_span(page, "FORT WORTH DIVISION").model_dump(),
            narrow_span(page, "THE BOEING COMPANY").model_dump()]


def test_a_quote_of_one_listed_span_is_completed_as_that_span(monkeypatch):
    calls = _script_replies(monkeypatch, {"quote": "THE BOEING\nCOMPANY"})

    output = _run(_quoting_stage(quoted_type="list[span]"), [_listed_spans()])

    assert _minted(output) == [Span.model_validate(_listed_spans()[1])]
    assert calls[0]["prompt"] == TEMPLATE.format(
        page='[\n  "FORT WORTH DIVISION",\n  "THE BOEING COMPANY"\n]')


def test_a_quote_that_is_no_listed_span_is_re_asked_then_fails_the_row(monkeypatch):
    calls = _script_replies(monkeypatch, {"quote": "BOEING"}, {"quote": "BOEING"})

    output = _run(_quoting_stage(quoted_type="list[span]", max_retries=1), [_listed_spans()])

    assert calls[1]["correction"] == (
        "quote is none of the 2 quotes in `page`; copy one of them whole: 'BOEING'")
    assert len(contribution_of(output).row_errors) == 1


# ── batch_size > 1 ──
def test_the_batched_path_asks_for_quotes_and_mints_each_item_alone(monkeypatch):
    calls: list[dict[str, Any]] = []

    def answer(stage_id, llm_config, *, instructions, task, reply_schema, usage_out):
        calls.append({"task": task})
        reply_schema.model_validate({"results": [{"row_number": 0, "basis": {"quote": "x"}}]})
        return script_judgment({"results": [
            {"row_number": 0, "basis": {"quote": "FORT WORTH DIVISION"}},
            {"row_number": 1, "basis": {"quote": "the plea was rejected"}}]})

    monkeypatch.setattr(lt, "call_llm_batch", answer)
    stage = _quoting_stage(batch_size=2)

    output = _run(stage, [_page_span().model_dump(), _page_span().model_dump()])

    assert len(calls) == 1
    assert PAGE_TEXT in calls[0]["task"]
    minted, refused = _minted(output)
    assert minted is not None and minted.quote == "FORT WORTH DIVISION"
    assert refused is None
    (error,) = contribution_of(output).row_errors
    assert (error["row"], error["message"]) == (1, (
        "quote not found on page 1 of us_v_boeing_ecf58_page1.pdf: 'the plea was rejected'"))


def test_the_stage_panel_shows_the_answer_as_a_verbatim_quote_from_its_column(tmp_path):
    set_parsed_stages(tmp_path / "demo", [
        parse_stage(source_stage("pages", [{"name": "page", "type": "span", "nullable": True}])),
        _quoting_stage()])

    html = TestClient(app).get("/project/demo/node/extract/panel").text

    answer_shape = html.split("expected answer shape")[1].split("</table>")[0]
    assert "A verbatim quote from <code>page</code>." in answer_shape


@pytest.mark.parametrize("batch_size", [1, 2])
def test_a_null_quote_leaves_the_span_null(monkeypatch, batch_size):
    monkeypatch.setattr(lt, "call_llm", lambda *a, **k: script_judgment({"basis": None}))
    monkeypatch.setattr(lt, "call_llm_batch", lambda *a, **k: script_judgment(
        {"results": [{"row_number": 0, "basis": None}]}))

    output = _run(_quoting_stage(batch_size=batch_size), [_page_span().model_dump()])

    assert _minted(output) == [None]
    assert not contribution_of(output).row_errors
