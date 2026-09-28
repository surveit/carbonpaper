"""A stage's span cells are verified against the files its run bound before its output lands."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from app.core.errors import SubsetRunError
from app.core.files import ProjectFile, compute_sha256, resolve_stored_path, save_upload
from app.core.frames import list_table_rows
from app.core.run_status import StageStatus
from app.core.text_sources import read_every_page_text, read_page_text
from app.models import Workflow, parse_stage
from app.models.locators import PageCharRange
from app.models.run_manifest import QUOTE_REFUSAL_ERROR_TYPE, RunKind, StageRecord
from app.models.spans import Span
from app.runtime import spans as spans_module
from app.runtime.context import RunIdentity
from app.runtime.executor import execute_subset
from app.runtime.manifest import read_run_manifest
from conftest import reads_of
from pdf_fixture import write_text_pdf

PROJECT = "boeing_docket"
RUN_ID = "r"
# https://storage.courtlistener.com/recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.58.0.pdf
ECF_58_PAGE_1 = Path(__file__).parent / "fixtures" / "us_v_boeing_ecf58_page1.pdf"
HEADING = "IN VIOLATION OF THE CRIME VICTIMS’ RIGHTS ACT"
FIRST_PAGE = "The first page says one thing."
SECOND_PAGE = "The second page says another."

_FILE_COLUMNS: list[dict[str, Any]] = [
    {"name": "source_id", "type": "str", "nullable": False},
    {"name": "source_sha256", "type": "str", "nullable": False},
]
_CLAIM_COLUMNS = [
    *_FILE_COLUMNS,
    {"name": "page", "type": "int", "nullable": False},
    {"name": "start", "type": "int", "nullable": False},
    {"name": "end", "type": "int", "nullable": False},
    {"name": "quote", "type": "str", "nullable": False},
]

_PAGE_COLUMNS = [
    {"name": "page", "type": "int", "nullable": False},
    {"name": "page_text", "type": "str", "nullable": False},
    {"name": "page_span", "type": "span", "nullable": False},
]

_WRITES_A_SPAN = """
from app.models.locators import PageCharRange
from app.models.spans import Span

def transform(row):
    locator = PageCharRange(page=row["page"], start=row["start"], end=row["end"])
    span = Span(source_id=row["source_id"], source_sha256=row["source_sha256"],
                locator=locator, quote=row["quote"])
    return dict(row, basis=span.model_dump())
"""

_WRITES_TEXT = """
def transform(row):
    return dict(row, basis=row["quote"])
"""


@pytest.fixture
def ecf_58() -> ProjectFile:
    with ECF_58_PAGE_1.open("rb") as stream:
        return save_upload(ECF_58_PAGE_1.name, stream, project_id=PROJECT)


def _page_text(record: ProjectFile) -> str:
    return read_page_text(resolve_stored_path(record), 1)


def _heading_claim(record: ProjectFile, **moved: int) -> dict[str, Any]:
    start = _page_text(record).index(HEADING)
    return {"source_id": record.id, "source_sha256": record.sha256, "page": 1,
            "start": start, "end": start + len(HEADING), "quote": HEADING, **moved}


def _input_stage(stage_id: str, columns: list[dict[str, Any]], paths: list[str]) -> dict[str, Any]:
    return {
        "id": stage_id, "description": stage_id, "type": "input_data",
        "connector": {"kind": "file", "params": {"paths": paths}},
        "signature": {"form": "replaces", "produces": columns},
    }


def _row_function(stage_id: str, code: str, basis_type: str) -> dict[str, Any]:
    return {
        "id": stage_id, "description": stage_id, "type": "python_row_function",
        "inputs": [{"id": "claims"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "claims", "columns": _CLAIM_COLUMNS}],
                      "adds": [{"name": "basis", "type": basis_type, "nullable": False}]},
        "function": {"kind": "inline", "code": code},
    }


def _run(
    paths: list[str], claims: list[dict[str, Any]], tmp_path: Path, *stages: dict[str, Any],
    identity: RunIdentity | None = RunIdentity(project=PROJECT, run_id=RUN_ID),
    kind: RunKind = RunKind.production,
) -> dict[str, pa.Table]:
    workflow = Workflow(stages=[parse_stage(spec) for spec in [_input_stage("claims", _CLAIM_COLUMNS, paths), *stages]])
    return execute_subset(
        workflow, injected_outputs={"claims": pd.DataFrame(claims)},
        stage_ids=[stage["id"] for stage in stages], run_dir=tmp_path / "runs" / RUN_ID,
        project_id=PROJECT, kind=kind, identity=identity)


def _stored_paths(record: ProjectFile) -> list[str]:
    return [str(resolve_stored_path(record))]


def _stage_record(stage_id: str, kind: RunKind = RunKind.production) -> StageRecord:
    records = read_run_manifest(PROJECT, RUN_ID, kind).stage_records
    return next(record for record in records if record.stage_id == stage_id)


def test_a_row_function_writing_a_quote_not_at_its_address_stops_the_stage(
    ecf_58: ProjectFile, tmp_path: Path,
) -> None:
    misplaced = _heading_claim(ecf_58, start=0, end=len(HEADING))
    with pytest.raises(SubsetRunError):
        _run(_stored_paths(ecf_58), [misplaced], tmp_path,
             _row_function("quote_heading", _WRITES_A_SPAN, "span"))

    record = _stage_record("quote_heading")
    assert record.status == StageStatus.ERROR and record.error is not None
    assert record.error.type == QUOTE_REFUSAL_ERROR_TYPE
    found = _page_text(ecf_58)[:len(HEADING)]
    assert record.error.message == (
        "stage 'quote_heading' column 'basis': 1 span(s) do not hold in the file they quote: "
        f"row 0: quote not at page 1 of {ECF_58_PAGE_1.name}: characters 0–{len(HEADING)} "
        f"hold {found!r}, not {HEADING!r}")


@pytest.mark.parametrize(("identity", "kind"), [
    (RunIdentity(project=PROJECT, run_id=RUN_ID), RunKind.production),
    (None, RunKind.eval),
])
def test_a_subset_run_binds_the_files_its_input_stages_name_so_its_spans_verify(
    ecf_58: ProjectFile, tmp_path: Path, identity: RunIdentity | None, kind: RunKind,
) -> None:
    claim = _heading_claim(ecf_58)
    outputs = _run(_stored_paths(ecf_58), [claim], tmp_path,
                   _row_function("quote_heading", _WRITES_A_SPAN, "span"),
                   identity=identity, kind=kind)

    assert _stage_record("quote_heading", kind).status == StageStatus.OK
    (row,) = list_table_rows(outputs["quote_heading"])
    assert Span.model_validate(row["basis"], strict=True) == Span(
        source_id=ecf_58.id, source_sha256=ecf_58.sha256,
        locator=PageCharRange(page=1, start=claim["start"], end=claim["end"]), quote=HEADING)


def test_a_span_on_a_file_no_input_stage_names_is_refused_as_never_read(
    ecf_58: ProjectFile, tmp_path: Path,
) -> None:
    with pytest.raises(SubsetRunError, match=f"this run read no file with sha256 {ecf_58.sha256}"):
        _run([], [_heading_claim(ecf_58)], tmp_path,
             _row_function("quote_heading", _WRITES_A_SPAN, "span"))

    error = _stage_record("quote_heading").error
    assert error is not None and error.type == QUOTE_REFUSAL_ERROR_TYPE


def test_a_page_is_read_once_per_run_however_many_stages_quote_it(
    ecf_58: ProjectFile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = _count_source_reads(monkeypatch)
    _run(_stored_paths(ecf_58), [_heading_claim(ecf_58)], tmp_path,
         _row_function("quote_heading", _WRITES_A_SPAN, "span"),
         _row_function("quote_it_again", _WRITES_A_SPAN, "span"))

    assert reads == {"pages": [1], "whole_files": [], "hashes": [ECF_58_PAGE_1.name]}


def test_a_stage_with_no_span_column_reads_no_source(
    ecf_58: ProjectFile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = _count_source_reads(monkeypatch)
    _run(_stored_paths(ecf_58), [_heading_claim(ecf_58)], tmp_path,
         _row_function("copy_heading", _WRITES_TEXT, "str"))

    assert _stage_record("copy_heading").status == StageStatus.OK
    assert reads == {"pages": [], "whole_files": [], "hashes": []}


def test_read_pages_page_spans_verify_from_the_one_extraction_read_pages_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = write_text_pdf(tmp_path / "two_pages.pdf", [FIRST_PAGE, SECOND_PAGE])
    with pdf.open("rb") as stream:
        record = save_upload(pdf.name, stream, project_id=PROJECT)
    files = _input_stage("files", _FILE_COLUMNS, _stored_paths(record))
    pages = {
        "id": "pages", "description": "Read each file a page at a time", "type": "read_pages",
        "inputs": [{"id": "files"}], "row_type_id": "file_page", "read_pages": {"carry": []},
        "signature": {"form": "replaces", "reads": reads_of("files", _FILE_COLUMNS),
                      "produces": [*_FILE_COLUMNS, *_PAGE_COLUMNS]},
    }
    reads = _count_source_reads(monkeypatch)
    outputs = execute_subset(
        Workflow(stages=[parse_stage(files), parse_stage(pages)]),
        injected_outputs={"files": pd.DataFrame(
            [{"source_id": record.id, "source_sha256": record.sha256}])},
        stage_ids=["pages"], run_dir=tmp_path / "runs" / RUN_ID, project_id=PROJECT,
        kind=RunKind.production, identity=RunIdentity(project=PROJECT, run_id=RUN_ID))

    assert _stage_record("pages").status == StageStatus.OK
    assert [row["page_text"] for row in list_table_rows(outputs["pages"])] == [
        FIRST_PAGE, SECOND_PAGE]
    assert reads == {"pages": [], "whole_files": [pdf.name], "hashes": [pdf.name]}


def _count_source_reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    reads: dict[str, list[Any]] = {"pages": [], "whole_files": [], "hashes": []}

    def read_and_count(path: Path, page: int) -> str:
        reads["pages"].append(page)
        return read_page_text(path, page)

    def read_every_page_and_count(path: Path) -> Iterator[str]:
        reads["whole_files"].append(path.name)
        return read_every_page_text(path)

    def hash_and_count(path: Path) -> str:
        reads["hashes"].append(path.name)
        return compute_sha256(path)

    monkeypatch.setattr(spans_module, "read_page_text", read_and_count)
    monkeypatch.setattr(spans_module, "read_every_page_text", read_every_page_and_count)
    monkeypatch.setattr(spans_module, "compute_sha256", hash_and_count)
    return reads
