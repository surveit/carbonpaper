from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.core.frames import list_table_rows, read_frame_table, table_from_rows, write_frame_table
from app.models import Column, TableSchema
from app.models.locators import CellAt, PageCharRange
from app.models.spans import Span
from app.runtime.validation import Issue, validate_table

_PAGE = "The court rejected the plea."
_CSV = "memo\npaid in full\n"


def _page_span() -> Span:
    return Span(source_id="stored_pdf", source_sha256=hashlib.sha256(_PAGE.encode()).hexdigest(),
                locator=PageCharRange(page=1, start=0, end=len(_PAGE)), quote=_PAGE)


def _cell_span() -> Span:
    return Span(source_id="stored_csv", source_sha256=hashlib.sha256(_CSV.encode()).hexdigest(),
                locator=CellAt(row=0, column="memo"), quote="paid in full")


def _find_issues(rows: list[dict[str, Any]], column_type: str = "span") -> list[Issue]:
    schema = TableSchema(columns=[Column(name="basis", type=column_type, nullable=True)])
    return validate_table(
        table_from_rows(rows), schema, stage_id="extract", phase="output").issues


def test_a_span_column_of_spans_has_no_issues() -> None:
    assert _find_issues([{"basis": _page_span().model_dump()}, {"basis": None}]) == []


def test_a_span_struct_missing_a_field_is_an_error() -> None:
    no_quote = {k: v for k, v in _page_span().model_dump().items() if k != "quote"}
    (issue,) = _find_issues([{"basis": no_quote}])
    assert (issue.severity, issue.column) == ("error", "basis")
    assert "1 value(s) do not read as a span" in issue.message
    assert "quote: Field required" in issue.message


def test_a_span_naming_an_unregistered_locator_kind_is_an_error() -> None:
    dump = _page_span().model_dump()
    dump["locator"] = {**dump["locator"], "kind": "ecf_page"}
    (issue,) = _find_issues([{"basis": dump}])
    assert issue.severity == "error"
    assert "no locator kind 'ecf_page' is registered" in issue.message


def test_a_list_of_spans_is_checked_element_by_element() -> None:
    good = _page_span().model_dump()
    bad = {**good, "quote": None}
    (issue,) = _find_issues([{"basis": [good, bad]}, {"basis": [good]}], "list[span]")
    assert "1 value(s) do not read as a span" in issue.message


def test_a_span_cell_survives_a_parquet_round_trip(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = [{"basis": _page_span().model_dump()}, {"basis": None}]
    path = tmp_path / "extract.parquet"
    write_frame_table(table_from_rows(rows), path)
    read_back = read_frame_table(path)
    assert list_table_rows(read_back) == rows
    schema = TableSchema(columns=[Column(name="basis", type="span", nullable=True)])
    assert validate_table(read_back, schema, stage_id="extract", phase="input").issues == []


def test_a_column_mixing_locator_kinds_reads_back_as_the_same_spans(tmp_path: Path) -> None:
    spans = [_page_span(), _cell_span()]
    path = tmp_path / "extract.parquet"
    write_frame_table(table_from_rows([{"basis": span.model_dump()} for span in spans]), path)
    rows = list_table_rows(read_frame_table(path))
    assert [Span.model_validate(row["basis"], strict=True) for row in rows] == spans
    assert _find_issues(rows) == []
