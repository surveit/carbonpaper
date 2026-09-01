"""Is a downloaded slice still a rectangle of the file the run read?"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pyarrow as pa

from app.core.errors import StageNotInRun
from app.core.frames import frame_to_table, read_frame_table
from app.core.source_files import read_source_file, resolve_file_format
from app.models.branch_analysis import RowOrdinal
from app.models.schema import StageId
from app.models.stages.input_data import FileConnectorParams
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir

import pytest

from app.services import run as run_service
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
    save_working_copy_as_version(PROJECT, message="fixture")
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


# A sheet line counts the header, so these rows have differing neighbours.
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
    save_working_copy_as_version(SHEET_PROJECT, message="fixture")
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


# The 1-based sheet line a file loader stamps on each row; absent, rows align by position.
SOURCE_ROW_COLUMN = "source_row"


@dataclass(frozen=True)
class SliceComparison:
    """`mismatches` counts cells, not rows: one wrong cell is one mismatch."""

    rows: int
    columns: int
    cells: int
    mismatches: int
    filename: str
    located_by: str


def compare_slice_to_the_file(project_id: str, run_id: str, stage_id: StageId,
                              ordinals: Sequence[RowOrdinal],
                              columns: Sequence[str]) -> SliceComparison:
    """Re-reads the file the way the run read it, then compares the slice cell by cell."""
    path = _read_the_binding(project_id, run_id, stage_id)
    ran = read_frame_table(
        resolve_run_dir(project_id, run_id) / "outputs" / f"{stage_id}.parquet")
    source = frame_to_table(_reread_the_source(project_id, run_id, stage_id, path))
    ran_rows = ran.take(pa.array(list(ordinals)))
    source_rows, located_by = _align_rows(
        ran_rows, source, _read_the_offset(project_id, run_id, stage_id), ordinals)
    mismatches = sum(_measure_differing_cells(ran_rows, source_rows, column)
                     for column in columns)
    return SliceComparison(rows=len(ordinals), columns=len(columns),
                           cells=len(ordinals) * len(columns), mismatches=mismatches,
                           filename=path.name, located_by=located_by)


def _reread_the_source(project_id: str, run_id: str, stage_id: StageId, path: Path):
    params = _read_the_connector_params(project_id, run_id, stage_id)
    return read_source_file(
        path, params.format or resolve_file_format(str(path)), dtype=str,
        sheet_name=params.sheet_name, header_row=params.header_row,
        first_column=params.first_column,
        source_row_column=params.source_row_column)


def _align_rows(ran_rows: pa.Table, source: pa.Table, skipped: int,
                ordinals: Sequence[RowOrdinal]) -> tuple[pa.Table, str]:
    """The file's rows in the slice's order, by stamped line where there is one."""
    if SOURCE_ROW_COLUMN in ran_rows.column_names \
            and SOURCE_ROW_COLUMN in source.column_names:
        at_line = {int(line): position for position, line
                   in enumerate(source.column(SOURCE_ROW_COLUMN).to_pylist())}
        wanted = [at_line[int(line)]
                  for line in ran_rows.column(SOURCE_ROW_COLUMN).to_pylist()]
        return source.take(pa.array(wanted)), SOURCE_ROW_COLUMN
    return (source.take(pa.array([skipped + ordinal for ordinal in ordinals])),
            "row position")


def _measure_differing_cells(ran_rows: pa.Table, source_rows: pa.Table,
                           column: str) -> int:
    ran = _render_as_text(ran_rows.column(column).to_pylist())
    reread = _render_as_text(source_rows.column(column).to_pylist())
    return sum(1 for here, there in zip(ran, reread) if here != there)


def _render_as_text(cells: list) -> list[str | None]:
    return [None if cell is None else str(cell) for cell in cells]


def _read_the_binding(project_id: str, run_id: str, stage_id: StageId) -> Path:
    binding = run_service.read_run_status(project_id, run_id)["input_bindings"].get(stage_id)
    if not binding:
        raise StageNotInRun(f"this run recorded no file for '{stage_id}'")
    files = binding.get("files") or [binding]
    return Path(files[0]["path"])


def _read_the_connector_params(project_id: str, run_id: str,
                               stage_id: StageId) -> FileConnectorParams:
    version = run_service.read_pinned_version(project_id, run_id)
    for authored in load_version_stages(project_id, version):
        connector = getattr(authored, "connector", None)
        if authored.id == stage_id and connector is not None:
            return connector.params
    raise StageNotInRun(f"the pinned version has no file stage '{stage_id}'")


def _read_the_offset(project_id: str, run_id: str, stage_id: StageId) -> int:
    offsets = run_service.read_run_status(project_id, run_id)["parameters"]["offsets"]
    return int(offsets.get(stage_id) or 0)
