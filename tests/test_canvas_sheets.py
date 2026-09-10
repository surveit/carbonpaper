"""The sheet under each canvas box: which rows it shows, in what order, and its counts."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
from app.web.loading import load_run_record
from app.web.scope_view import read_run_branches
from app.web.sheet_preview import CELL_CHARS, PREVIEW_ROWS, render_sheet_cell
from app.web.values_view import build_trace_scope, load_values_used
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "canvas_fixture"
# The figure every test cites: the grand total, which five grants feed.
CITED = ("grant_totals", "total_amount", 0)


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


@pytest.fixture
def values(run_id):
    return load_values_used(PROJECT, run_id, *CITED)


def _sheet(values, stage_id):
    return next(sheet for sheet in values.sheets if sheet.stage_id == stage_id)


def _rows_behind(run_id, stage_id):
    return build_trace_scope(PROJECT, run_id, *CITED).read_rows_at(stage_id)


def test_one_sheet_per_frame_writing_stage_in_run_order(run_id, values):
    record = load_run_record(PROJECT, run_id)
    assert [sheet.stage_id for sheet in values.sheets] == [
        entry.stage_id for entry in record.stage_records if entry.output_path]


def test_a_filter_counts_what_went_in_what_came_out_and_what_it_dropped(values):
    funded = _sheet(values, "funded")
    assert (funded.rows_in, funded.rows_out, funded.rows_dropped) == (10, 9, 1)
    grants_only = _sheet(values, "grants_only")
    assert (grants_only.rows_in, grants_only.rows_out, grants_only.rows_dropped) == (8, 5, 3)
    assert grants_only.type == "filter_rows"


def test_a_filter_off_the_figures_route_still_counts_what_it_dropped(values):
    # No cut is recorded off the route, so the count comes off the filter's own diff.
    sheet = _sheet(values, "over_a_million")
    assert (sheet.rows_in, sheet.rows_out, sheet.rows_dropped) == (5, 0, 5)


def test_a_filter_sheet_leads_with_the_figures_rows_then_kept_then_dropped(run_id, values):
    # All five kept rows are the figure's; the three loans fill the sheet after them.
    sheet = _sheet(values, "grants_only")
    assert sheet.rows_behind == 5
    assert [row.ordinal for row in sheet.rows[:5]] == _rows_behind(run_id, "grants_only")
    assert all(row.mine and not row.dropped for row in sheet.rows[:5])
    assert [row.dropped for row in sheet.rows[5:]] == [True, True, True]
    assert all(row.ordinal is None and not row.mine for row in sheet.rows[5:])
    kinds = [row.cells[sheet.columns.index("kind")] for row in sheet.rows]
    assert kinds == ["grant"] * 5 + ["loan"] * 3


def test_a_dropped_row_waits_until_the_kept_rows_run_out(run_id, values):
    # Nine kept rows fill the eight slots, so the zero-amount grant is not among them.
    sheet = _sheet(values, "funded")
    behind = _rows_behind(run_id, "funded")
    assert len(sheet.rows) == PREVIEW_ROWS
    assert [row.ordinal for row in sheet.rows[:len(behind)]] == behind
    assert [row.mine for row in sheet.rows] == (
        [True] * len(behind) + [False] * (PREVIEW_ROWS - len(behind)))
    assert not any(row.dropped for row in sheet.rows)


def test_an_ordinary_sheet_leads_with_the_figures_rows_then_the_frames_first(run_id, values):
    sheet = _sheet(values, "size_band")
    behind = _rows_behind(run_id, "size_band")
    assert (sheet.rows_in, sheet.rows_out, sheet.rows_dropped) == (10, 10, 0)
    assert [row.ordinal for row in sheet.rows[:len(behind)]] == behind
    assert all(row.mine for row in sheet.rows[:len(behind)])
    rest = [ordinal for ordinal in range(10) if ordinal not in behind]
    assert [row.ordinal for row in sheet.rows[len(behind):]] == (
        rest[:PREVIEW_ROWS - len(behind)])
    assert not any(row.dropped or (row.mine and row.ordinal not in behind)
                   for row in sheet.rows)


def test_the_cited_stage_shows_its_one_row_as_the_figures(values):
    sheet = _sheet(values, "grant_totals")
    assert sheet.rows_out == 1
    assert [(row.ordinal, row.mine) for row in sheet.rows] == [(0, True)]
    assert sheet.rows[0].cells[sheet.columns.index("total_amount")] == "2200"


def test_a_cut_carries_the_branchs_recorded_count_not_its_label(run_id, values):
    # The dedupe took out west's copy of G-004, so it is a cut as much as a filter is.
    run_branches = read_run_branches(PROJECT, run_id)
    for cut in values.cuts:
        assert cut.rows == run_branches.row_count_per_branch_id[cut.branch]
    assert {cut.stage_id: cut.rows for cut in values.cuts} == {
        "funded": 1, "one_row_per_grant": 1, "grants_only": 3}
    deduped = _sheet(values, "one_row_per_grant")
    assert (deduped.rows_in, deduped.rows_out, deduped.rows_dropped) == (9, 8, 1)


def test_a_cell_is_clipped_to_the_constant():
    assert render_sheet_cell("x" * 200) == "x" * (CELL_CHARS - 1) + "…"
    assert len(render_sheet_cell("x" * 200)) == CELL_CHARS
    assert render_sheet_cell("x" * CELL_CHARS) == "x" * CELL_CHARS
    assert render_sheet_cell(None) == ""
    assert render_sheet_cell(2200) == "2200"


def test_every_cell_on_every_sheet_is_within_the_constant(values):
    for sheet in values.sheets:
        for row in sheet.rows:
            assert len(row.cells) == len(sheet.columns)
            assert all(len(cell) <= CELL_CHARS for cell in row.cells)


def test_the_pane_hands_its_script_the_sheets_and_not_the_cuts_behind_them(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=grant_totals&row=0&column=total_amount")
    assert page.status_code == 200
    assert '"sheets"' in page.text
    assert '"rows_dropped"' in page.text
    # The cuts are what the dropped counts were read off; the script draws the counts.
    assert '"cuts"' not in page.text
