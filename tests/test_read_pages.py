"""read_pages: one row per page of each file its run read, each row quoting its whole page."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from app.core.errors import SourceChanged, SourceIdMismatch, SourceNotRead
from app.core.files import ProjectFile, resolve_stored_path, save_upload
from app.core.frames import list_table_rows
from app.core.text_sources import normalize_text
from app.models import Workflow, parse_stage
from app.models.locators import PageCharRange
from app.models.spans import Span
from app.models.workflow import validate_workflow_draft
from app.runtime.context import RunContext
from app.runtime.runner import prepare_run
from app.runtime.spans import SourceTextCache, verify_span
from app.runtime.stage_output import StageOutput
from app.runtime.stages.read_pages import handle_read_pages
from conftest import as_inputs, place_stage, reads_of, source_stage
from pdf_fixture import write_text_pdf

PROJECT = "boeing_docket"
FIRST_PAGE = "The government moves to dismiss the information."
SECOND_PAGE = "The families oppose the motion."
# https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.58.0.pdf
ECF_58_PAGE_1 = Path(__file__).parent / "fixtures" / "us_v_boeing_ecf58_page1.pdf"

_SOURCE_ID = {"name": "source_id", "type": "str", "nullable": False}
_SHA256 = {"name": "source_sha256", "type": "str", "nullable": False}
_ECF_ENTRY = {"name": "ecf_entry", "type": "int", "nullable": False}
_FILING_COLUMNS = [_ECF_ENTRY, _SOURCE_ID, _SHA256]
_PAGE_COLUMNS = [
    {"name": "page", "type": "int", "nullable": False},
    {"name": "page_text", "type": "str", "nullable": False},
    {"name": "page_span", "type": "span", "nullable": False},
]


def _read_pages_spec(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "filing_pages", "description": "Read each filing a page at a time",
        "type": "read_pages", "inputs": [{"id": "filings"}], "row_type_id": "filing_page",
        "signature": {"form": "replaces", "reads": reads_of("filings", _FILING_COLUMNS),
                      "produces": [*_FILING_COLUMNS, *_PAGE_COLUMNS]},
        "read_pages": {"carry": ["ecf_entry"]},
        **overrides,
    }


def _store(path: Path) -> ProjectFile:
    with path.open("rb") as stream:
        return save_upload(path.name, stream, project_id=PROJECT)


def _filing_row(ecf_entry: int, record: ProjectFile) -> dict[str, Any]:
    return {"ecf_entry": ecf_entry, "source_id": record.id, "source_sha256": record.sha256}


def _bind(tmp_path: Path, *records: ProjectFile) -> RunContext:
    """The run's context once prepare_run has bound each stored file `filings` reads."""
    stored_paths = [str(resolve_stored_path(record)) for record in records]
    filings = {**source_stage("filings", _FILING_COLUMNS),
               "connector": {"kind": "file", "params": {"paths": stored_paths}}}
    workflow = Workflow(stages=[parse_stage(filings), parse_stage(_read_pages_spec())])
    ctx: RunContext = prepare_run(tmp_path / "runs", PROJECT, workflow, "v1")["ctx"]
    return ctx


def _read(ctx: RunContext, rows: list[dict[str, Any]]) -> StageOutput:
    stage = place_stage(parse_stage(_read_pages_spec()))
    return handle_read_pages(stage, as_inputs({"filings": pd.DataFrame(rows)}), ctx)


def test_a_two_page_pdf_becomes_two_rows_each_quoting_its_whole_page(tmp_path: Path) -> None:
    record = _store(write_text_pdf(tmp_path / "ecf_58.pdf", [FIRST_PAGE, SECOND_PAGE]))
    ctx = _bind(tmp_path, record)

    rows = list_table_rows(_read(ctx, [_filing_row(58, record)]).table)

    assert [(row["ecf_entry"], row["page"], row["page_text"]) for row in rows] == [
        (58, 1, FIRST_PAGE), (58, 2, SECOND_PAGE)]
    spans = [Span.model_validate(row["page_span"], strict=True) for row in rows]
    assert spans[1] == Span(
        source_id=record.id, source_sha256=record.sha256,
        locator=PageCharRange(page=2, start=0, end=len(SECOND_PAGE)), quote=SECOND_PAGE)
    for span in spans:
        # A fresh cache reads the page off disk again, rather than the text read_pages kept.
        verify_span(span, ctx.bound_sources, SourceTextCache())


def test_each_page_row_names_the_file_row_it_was_read_from(tmp_path: Path) -> None:
    motion = _store(write_text_pdf(tmp_path / "ecf_17.pdf", [FIRST_PAGE, SECOND_PAGE]))
    order = _store(write_text_pdf(tmp_path / "ecf_282.pdf", [FIRST_PAGE]))

    output = _read(_bind(tmp_path, motion, order),
                   [_filing_row(17, motion), _filing_row(282, order)])

    assert output.lineage is not None
    assert output.lineage.read_parent_ordinals("filings") == [0, 0, 1]


def test_the_ecf_stamp_reads_off_the_page_text_of_a_real_docket_page(tmp_path: Path) -> None:
    record = _store(ECF_58_PAGE_1)

    (row,) = list_table_rows(_read(_bind(tmp_path, record), [_filing_row(58, record)]).table)

    stamp = "Case 4:21-cr-00005-O Document 58 Filed 02/08/22 Page 1 of 26 PageID 536"
    assert stamp in normalize_text(row["page_text"])


def test_a_page_with_no_text_layer_is_empty_and_warns_naming_the_file(tmp_path: Path) -> None:
    record = _store(write_text_pdf(tmp_path / "exhibit_a.pdf", [FIRST_PAGE, ""]))

    output = _read(_bind(tmp_path, record), [_filing_row(18, record)])

    assert [row["page_text"] for row in list_table_rows(output.table)] == [FIRST_PAGE, ""]
    assert output.contribution.warnings == [
        "'exhibit_a.pdf' has no text layer on page(s) 2 of 2, so nothing on them can be quoted"]


def test_a_file_the_run_did_not_read_is_refused(tmp_path: Path) -> None:
    motion = _store(write_text_pdf(tmp_path / "ecf_17.pdf", [FIRST_PAGE]))
    unread = _store(write_text_pdf(tmp_path / "ecf_18.pdf", [SECOND_PAGE]))

    with pytest.raises(SourceNotRead, match=f"read no file with sha256 {unread.sha256}"):
        _read(_bind(tmp_path, motion), [_filing_row(18, unread)])


def test_a_source_id_other_than_the_file_the_run_read_is_refused(tmp_path: Path) -> None:
    record = _store(write_text_pdf(tmp_path / "ecf_58.pdf", [FIRST_PAGE]))
    row = {**_filing_row(58, record), "source_id": "ecf_404"}

    with pytest.raises(SourceIdMismatch, match=(
            f"source 'ecf_404' is not the file this run read: it read ecf_58.pdf as stored "
            f"file '{record.id}'")):
        _read(_bind(tmp_path, record), [row])


def test_bytes_changed_since_the_run_read_them_are_refused(tmp_path: Path) -> None:
    record = _store(write_text_pdf(tmp_path / "ecf_58.pdf", [FIRST_PAGE]))
    ctx = _bind(tmp_path, record)
    write_text_pdf(resolve_stored_path(record), [SECOND_PAGE])

    with pytest.raises(SourceChanged, match="ecf_58.pdf now hashes to"):
        _read(ctx, [_filing_row(58, record)])


def test_an_execution_that_read_no_files_is_refused_before_any_row(tmp_path: Path) -> None:
    record = _store(write_text_pdf(tmp_path / "ecf_58.pdf", [FIRST_PAGE]))

    with pytest.raises(SourceNotRead, match="reads only the files its run's input stages read"):
        _read(RunContext.for_stages_outside_a_run(tmp_path), [_filing_row(58, record)])



def test_carry_may_not_name_a_column_read_pages_writes() -> None:
    with pytest.raises(ValidationError, match="read_pages.carry names `page`"):
        parse_stage(_read_pages_spec(read_pages={"carry": ["page"]}))


def test_produces_must_hold_every_column_read_pages_writes() -> None:
    produces_no_span = [*_FILING_COLUMNS, *_PAGE_COLUMNS[:2]]
    spec = _read_pages_spec(signature={
        "form": "replaces", "reads": reads_of("filings", _FILING_COLUMNS),
        "produces": produces_no_span})

    (issue,) = validate_workflow_draft([source_stage("filings", _FILING_COLUMNS), spec])

    assert issue.endswith(
        "stage 'filing_pages': read_pages writes `page_span` but the signature's produces omits it")


def test_a_carried_column_must_come_from_the_input() -> None:
    spec = _read_pages_spec(read_pages={"carry": ["ecf_entry", "date_filed"]})

    issues = "; ".join(validate_workflow_draft([source_stage("filings", _FILING_COLUMNS), spec]))

    assert "read_pages.carry references column 'date_filed' not in its input schema" in issues


def test_the_signature_reads_both_columns_that_name_the_file() -> None:
    spec = _read_pages_spec(signature={
        "form": "replaces", "reads": reads_of("filings", [_ECF_ENTRY, _SOURCE_ID]),
        "produces": [*_FILING_COLUMNS, *_PAGE_COLUMNS]})

    (issue,) = validate_workflow_draft([source_stage("filings", _FILING_COLUMNS), spec])

    assert issue.endswith(
        "read_pages consumes `source_sha256` but the signature does not read it")
