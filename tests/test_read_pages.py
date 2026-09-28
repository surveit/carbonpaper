"""read_pages: one row per page of each stored file, each row quoting its whole page."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest
from pydantic import ValidationError

from app.core.errors import FileNotStoredError
from app.core.files import ProjectFile, resolve_stored_path, save_upload
from app.core.frames import list_table_rows
from app.core.text_sources import normalize_text, read_page_text
from app.models import Workflow, parse_stage
from app.models.locators import PageCharRange
from app.models.run_manifest import RunKind
from app.models.spans import Span
from app.models.workflow import validate_workflow_draft
from app.runtime.context import RunContext, RunIdentity
from app.runtime.errors import SourceSha256Mismatch
from app.runtime.executor import execute_subset
from app.runtime.manifest import read_run_manifest
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


def _run(tmp_path: Path, rows: list[dict[str, Any]]) -> pa.Table:
    workflow = Workflow(stages=[
        parse_stage(source_stage("filings", _FILING_COLUMNS)), parse_stage(_read_pages_spec()),
    ])
    outputs = execute_subset(
        workflow, injected_outputs={"filings": pd.DataFrame(rows)},
        stage_ids=["filing_pages"], run_dir=tmp_path / "runs" / "r", project_id=PROJECT,
        kind=RunKind.production, identity=RunIdentity(project=PROJECT, run_id="r"))
    return outputs["filing_pages"]


def _read_directly(tmp_path: Path, rows: list[dict[str, Any]]) -> StageOutput:
    ctx = RunContext.for_workflow_test_run(tmp_path, PROJECT, "r")
    stage = place_stage(parse_stage(_read_pages_spec()))
    return handle_read_pages(stage, as_inputs({"filings": pd.DataFrame(rows)}), ctx)


def test_a_two_page_pdf_becomes_two_rows_each_quoting_its_whole_page(tmp_path: Path) -> None:
    record = _store(write_text_pdf(tmp_path / "ecf_58.pdf", [FIRST_PAGE, SECOND_PAGE]))

    rows = list_table_rows(_run(tmp_path, [_filing_row(58, record)]))

    assert [(row["ecf_entry"], row["page"], row["page_text"]) for row in rows] == [
        (58, 1, FIRST_PAGE), (58, 2, SECOND_PAGE)]
    spans = [Span.model_validate(row["page_span"], strict=True) for row in rows]
    assert spans[1] == Span(
        source_id=record.id, source_sha256=record.sha256,
        locator=PageCharRange(page=2, start=0, end=len(SECOND_PAGE)), quote=SECOND_PAGE)
    for span in spans:
        assert isinstance(span.locator, PageCharRange)
        page_text = read_page_text(resolve_stored_path(record), span.locator.page)
        assert page_text[span.locator.start:span.locator.end] == span.quote


def test_each_page_row_names_the_file_row_it_was_read_from(tmp_path: Path) -> None:
    motion = _store(write_text_pdf(tmp_path / "ecf_17.pdf", [FIRST_PAGE, SECOND_PAGE]))
    order = _store(write_text_pdf(tmp_path / "ecf_282.pdf", [FIRST_PAGE]))

    output = _read_directly(tmp_path, [_filing_row(17, motion), _filing_row(282, order)])

    assert output.lineage is not None
    assert output.lineage.read_parent_ordinals("filings") == [0, 0, 1]


def test_the_ecf_stamp_reads_off_the_page_text_of_a_real_docket_page(tmp_path: Path) -> None:
    record = _store(ECF_58_PAGE_1)

    (row,) = list_table_rows(_run(tmp_path, [_filing_row(58, record)]))

    stamp = "Case 4:21-cr-00005-O Document 58 Filed 02/08/22 Page 1 of 26 PageID 536"
    assert stamp in normalize_text(row["page_text"])


def test_a_page_with_no_text_layer_is_empty_and_warns_naming_the_file(tmp_path: Path) -> None:
    record = _store(write_text_pdf(tmp_path / "exhibit_a.pdf", [FIRST_PAGE, ""]))

    rows = list_table_rows(_run(tmp_path, [_filing_row(18, record)]))

    assert [row["page_text"] for row in rows] == [FIRST_PAGE, ""]
    (stage_record,) = read_run_manifest(PROJECT, "r", RunKind.production).stage_records
    assert stage_record.output_validation_report is not None
    assert stage_record.output_validation_report["issues"] == [{
        "severity": "warning", "column": None,
        "message": "'exhibit_a.pdf' has no text layer on page(s) 2 of 2, so nothing on them "
                   "can be quoted",
    }]


def test_a_source_id_the_project_does_not_hold_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotStoredError, match="has no file 'ecf_404'"):
        _read_directly(tmp_path, [{"ecf_entry": 404, "source_id": "ecf_404",
                                   "source_sha256": "0" * 64}])


def test_bytes_that_do_not_hash_to_the_rows_sha256_are_refused(tmp_path: Path) -> None:
    record = _store(write_text_pdf(tmp_path / "ecf_58.pdf", [FIRST_PAGE]))
    row = {**_filing_row(58, record), "source_sha256": "0" * 64}

    with pytest.raises(SourceSha256Mismatch, match=f"hashes to {record.sha256}, not the 0000"):
        _read_directly(tmp_path, [row])


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
