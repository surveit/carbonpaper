"""One input reading two xlsx files: the Input files view names each row's own file and sheet row."""
from __future__ import annotations

import openpyxl
import pytest

from app.models.citations import StageOutputCellCitation
from app.services import run as run_service
from app.web.input_files_view import InputFileSlice, load_input_files
from scope_fixture import column
from stage_seed import save_version, set_stages

PROJECT = "input_files_two_quarters"


def _write_quarter(path, filings: list[tuple[str, int]]) -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["filing", "amount"])
    for filing in filings:
        sheet.append(list(filing))
    book.save(path)


def _stage_specs(q1, q2) -> list[dict]:
    return [
        {
            "id": "load_filings", "type": "input_data",
            "description": "Both quarters' filings, read as one table.",
            "connector": {"kind": "file", "params": {
                "paths": [str(q1), str(q2)], "format": "xlsx",
                "source_row_column": "export_row"}},
            "signature": {"form": "replaces", "produces": [
                column("filing", "str", False), column("amount", "int", False),
                column("export_row", "int", False)]},
        },
        {
            "id": "paid", "type": "filter_rows",
            "description": "Keeps the filings that report money.",
            "inputs": [{"id": "load_filings"}],
            "filter": {"predicate": "pass this step's test",
                       "summary": "Keeps a filing only where its amount is above zero.",
                       "corner_cases": [{"case": "amount is 0",
                                         "expected": "the row is dropped"}],
                       "code": 'def should_include(row):\n    return row["amount"] > 0\n'},
            "signature": {"form": "extends", "reads": [
                {"input": "load_filings", "columns": [column("amount", "int", False)]}],
                "adds": [], "rewrites": []},
        },
        {
            "id": "paid_total", "type": "aggregate",
            "description": "One row: what the paid filings come to.",
            "inputs": [{"id": "paid"}],
            "aggregate": {"group_by": [], "aggregations": [
                {"output_column": "total_amount", "formula": "sum",
                 "value_column": "amount"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "paid", "columns": [column("amount", "int", False)]}],
                "produces": [column("total_amount", "int")]},
        },
    ]


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    data.mkdir(parents=True)
    _write_quarter(data / "q1.xlsx", [("Q1-A", 0), ("Q1-B", 100), ("Q1-C", 300)])
    _write_quarter(data / "q2.xlsx", [("Q2-A", 500), ("Q2-B", 0)])
    set_stages(PROJECT, _stage_specs(data / "q1.xlsx", data / "q2.xlsx"))
    save_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def _read_the_label_of_each_relevant_filing(one: InputFileSlice) -> dict[str, str]:
    filing = one.columns_read.index("filing")
    return {str(row.cells[filing]): row.label for row in one.rows if row.relevant}


def test_a_row_from_each_file_is_named_by_its_own_file_and_sheet_row(run_id):
    view = load_input_files(PROJECT, run_id, StageOutputCellCitation(
        run_id=run_id, stage_id="paid_total", row_ordinal=0, column="total_amount",
        value=None))
    q1, q2 = view.files
    assert (q1.filename, q1.rows_read, q1.row_label) == ("q1.xlsx", 3, "sheet row")
    assert (q2.filename, q2.rows_read, q2.row_label) == ("q2.xlsx", 2, "sheet row")
    # The header is sheet row 1, so a file's first filing sits on sheet row 2.
    assert _read_the_label_of_each_relevant_filing(q1) == {"Q1-B": "3", "Q1-C": "4"}
    assert _read_the_label_of_each_relevant_filing(q2) == {"Q2-A": "2"}
