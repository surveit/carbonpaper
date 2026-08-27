"""The Input files view over the twelve rows tests/scope_fixture.py writes."""
from __future__ import annotations

import pytest

from app.models.claims import StageOutputCellCitation
from app.services import run as run_service
from app.services.project import save_working_copy_as_version
from app.web.input_files_view import load_input_files
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "input_files_fixture"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture", reviewer="test")
    return str(run_service.execute(PROJECT)["run_id"])


def _view(run_id: str, stage="grant_totals", column="total_amount", row=0):
    return load_input_files(PROJECT, run_id, StageOutputCellCitation(
        run_id=run_id, stage_id=stage, row_ordinal=row, column=column, value=None))


def test_it_lists_every_file_the_rows_passed_through(run_id):
    view = _view(run_id)
    assert [one.stage_id for one in view.files] == [
        "load_east", "load_west", "load_agencies"]


def test_a_lookup_carries_the_key_it_matched_on_and_nothing_it_only_offered(run_id):
    lookup = next(one for one in _view(run_id).files
                  if one.stage_id == "load_agencies")
    assert lookup.columns_relevant == ["agency_code"]
    assert "portfolio" in lookup.columns_read


def test_the_cited_value_is_read_back_off_the_frame(run_id):
    assert _view(run_id).value == 2200


def test_a_file_carries_both_shapes_and_the_columns_that_mattered(run_id):
    east = next(one for one in _view(run_id).files if one.stage_id == "load_east")
    assert east.filename == "east.csv"
    assert east.rows_read == 6
    assert east.rows_relevant < east.rows_read
    assert set(east.columns_relevant) < set(east.columns_read)
    assert len(east.shape_over_relevant_rows) == len(east.columns_read)
    assert len(east.shape_over_every_row) == len(east.columns_read)


def test_the_shape_over_the_relevant_rows_is_not_the_shape_over_the_frame(run_id):
    east = next(one for one in _view(run_id).files if one.stage_id == "load_east")
    over_all = {one.column: one.distinct_count for one in east.shape_over_every_row}
    over_few = {one.column: one.distinct_count for one in east.shape_over_relevant_rows}
    assert over_few["grant_id"] < over_all["grant_id"]


def test_the_preview_leads_with_the_relevant_rows(run_id):
    east = next(one for one in _view(run_id).files if one.stage_id == "load_east")
    assert [row.relevant for row in east.rows][:east.rows_relevant] \
        == [True] * east.rows_relevant
    assert east.row_label == "row"


def test_an_uncapped_read_names_no_cap_and_no_file_total(run_id):
    east = next(one for one in _view(run_id).files if one.stage_id == "load_east")
    assert east.cap is None
    assert east.rows_in_file is None
