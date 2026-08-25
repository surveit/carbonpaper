"""The subset check against the CSVs tests/scope_fixture.py writes and the run reads."""
from __future__ import annotations

import pytest

from app.services import run as run_service
from app.services.input_check import compare_slice_to_the_file
from app.services.scope import find_rows_reached_per_stage
from app.services.project import save_working_copy_as_version
from app.web.scope_view import read_run_branches
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "subset_check_fixture"
SHEET_PROJECT = "subset_check_sheet"
GRANT_COLUMNS = ["grant_id", "agency_code", "amount"]


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture", reviewer="test")
    return str(run_service.execute(PROJECT)["run_id"])


def _rows_behind(run_id: str, stage: str) -> list[int]:
    branches = read_run_branches(PROJECT, run_id)
    reached = find_rows_reached_per_stage(branches, [("grant_totals", 0)])
    return sorted(reached[stage])


def test_the_rows_behind_a_total_are_a_rectangle_of_the_file_they_came_from(run_id):
    ordinals = _rows_behind(run_id, "load_east")
    checked = compare_slice_to_the_file(PROJECT, run_id, "load_east", ordinals,
                                       GRANT_COLUMNS)
    assert checked.rows == len(ordinals)
    assert checked.cells == len(ordinals) * 3
    assert checked.mismatches == 0
    assert checked.filename == "east.csv"
    assert checked.located_by == "row position"


def test_every_row_of_the_file_is_a_rectangle_of_it_too(run_id):
    checked = compare_slice_to_the_file(PROJECT, run_id, "load_east", list(range(6)),
                                       GRANT_COLUMNS)
    assert checked.cells == 18
    assert checked.mismatches == 0


def test_a_stage_the_run_read_no_file_for_is_refused(run_id):
    with pytest.raises(Exception):
        compare_slice_to_the_file(PROJECT, run_id, "grant_totals", [0], ["total_amount"])


# The xlsx path is the one that stamps `source_row`, and a sheet line counts the
# header, so an off-by-one here would compare a row against its neighbour.
SHEET = [("G-101", "AGENCY-A", 100), ("G-102", "AGENCY-B", 200),
         ("G-103", "AGENCY-A", 300), ("G-104", "AGENCY-C", 400)]


@pytest.fixture
def sheet_run_id(projects_root):
    import pandas as pd
    data = projects_root / SHEET_PROJECT / "data"
    data.mkdir(parents=True, exist_ok=True)
    book = data / "grants.xlsx"
    pd.DataFrame(SHEET, columns=["grant_id", "agency_code", "amount"]).to_excel(
        book, sheet_name="Sheet1", index=False)
    set_stages(SHEET_PROJECT, _sheet_stages(book))
    save_working_copy_as_version(SHEET_PROJECT, message="fixture", reviewer="test")
    return str(run_service.execute(SHEET_PROJECT)["run_id"])


def _sheet_stages(book) -> list[dict]:
    columns = [{"name": "grant_id", "type": "str", "nullable": False},
               {"name": "agency_code", "type": "str", "nullable": False},
               {"name": "amount", "type": "int", "nullable": False},
               {"name": "source_row", "type": "int", "nullable": False}]
    return [{
        "id": "load_sheet", "type": "input_data", "cache": True,
        "description": "Grants as the spreadsheet lists them.",
        "connector": {"kind": "file", "params": {
            "paths": [str(book)], "format": "xlsx", "sheet_name": "Sheet1",
            "header_row": 0, "source_row_column": "source_row"}},
        "signature": {"form": "replaces", "produces": columns},
    }]


def test_a_sheet_row_locates_by_its_line_and_still_matches(sheet_run_id):
    checked = compare_slice_to_the_file(SHEET_PROJECT, sheet_run_id, "load_sheet",
                                       [1, 3], ["grant_id", "amount"])
    assert checked.located_by == "source_row"
    assert checked.cells == 4
    assert checked.mismatches == 0
