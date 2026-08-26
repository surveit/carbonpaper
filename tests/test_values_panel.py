"""The Values used tab, over the twelve-row grants workflow in scope_fixture."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
from app.web.diff_state import ColumnDiffState
from app.web.values_view import load_values_used
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "values_fixture"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture", reviewer="test")
    return str(run_service.execute(PROJECT)["run_id"])


def _walk(run_id, stage, column):
    return load_values_used(PROJECT, run_id, stage, column, row=0)


def _step(values, stage_id):
    return next(step for step in values.steps if step.stage_id == stage_id)


def _diff_column(step, name):
    return next(column for column in step.diff.columns if column.name == name)


def test_only_the_stages_that_wrote_get_a_step(run_id):
    # The six stages between pass `amount` through untouched.
    values = _walk(run_id, "by_portfolio", "total_amount")
    assert [step.stage_id for step in values.steps] == [
        "load_east", "load_west", "by_portfolio"]


def test_a_union_below_a_run_of_pass_throughs_is_still_the_fork(run_id):
    values = _walk(run_id, "by_portfolio", "total_amount")
    assert [source.stage_id for source in values.sources["by_portfolio"]] == [
        "load_east", "load_west"]


def test_the_enrich_that_added_the_group_key_is_on_the_walk(run_id):
    values = _walk(run_id, "by_portfolio", "portfolio")
    assert "tag_portfolio" in [step.stage_id for step in values.steps]


def test_a_column_is_on_no_sheet_before_the_stage_that_writes_it(run_id):
    values = _walk(run_id, "by_portfolio", "portfolio")
    assert "portfolio" not in [c.name for c in _step(values, "load_east").columns]
    portfolio = _diff_column(_step(values, "tag_portfolio"), "portfolio")
    assert portfolio.state is ColumnDiffState.added


def test_the_column_the_stage_read_is_drawn_beside_what_it_wrote(run_id):
    values = _walk(run_id, "by_portfolio", "portfolio")
    agency_code = _diff_column(_step(values, "tag_portfolio"), "agency_code")
    assert agency_code.state is ColumnDiffState.carried


def test_a_column_the_stage_read_is_not_dimmed_with_the_rest(run_id):
    # tag_portfolio joins on agency_code; dimming it would hide what the join used.
    step = _step(_walk(run_id, "by_portfolio", "portfolio"), "tag_portfolio")
    assert _diff_column(step, "agency_code").inert is False


def test_a_transform_step_carries_the_input_to_output_split(run_id):
    values = _walk(run_id, "by_portfolio", "portfolio")
    step = _step(values, "tag_portfolio")
    assert [frame.role for frame in step.diff.inputs] == ["base input", "reference input"]
    assert "+1 col" in step.diff.count_labels


def test_an_input_stage_has_no_frame_to_paint_over(run_id):
    values = _walk(run_id, "by_portfolio", "portfolio")
    assert _step(values, "load_east").diff is None


def _minimap_columns(values):
    return [[node.stage_id for node in column] for column in values.minimap]


def test_two_sources_of_one_stage_stack_rather_than_read_as_a_chain(run_id):
    # Drawn one after another they read as the first flowing into the second.
    columns = _minimap_columns(_walk(run_id, "by_portfolio", "portfolio"))
    assert set(columns[0]) == {"load_agencies", "load_east", "load_west"}
    assert columns[1:] == [["tag_portfolio"], ["by_portfolio"]]


def test_an_aggregate_hands_back_a_new_sheet_and_says_which(run_id):
    values = _walk(run_id, "by_portfolio", "total_amount")
    assert _step(values, "by_portfolio").new_sheet == "per_group"
    assert _walk(run_id, "grant_totals", "total_amount").steps[-1].new_sheet == "one_row"


def test_a_count_reads_no_column_so_the_tab_says_so(run_id):
    assert _walk(run_id, "grant_totals", "grants").counts_rows is True


def test_the_panel_renders_the_sheet_of_every_step(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=by_portfolio&row=0&column=total_amount")
    assert page.status_code == 200
    assert page.text.count('class="vu-step"') == 3
    assert 'data-transform="load_east"' in page.text
    # The shared diff table, not a second one of this tab's own.
    assert 'class="data-preview"' in page.text


def test_a_column_the_stage_does_not_write_is_refused_in_the_pane(run_id):
    # A pane that 404s shows the reader a browser error page inside a tab.
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=by_portfolio&row=0&column=nothing_here")
    assert page.status_code == 200
    assert "writes no column" in page.text


def _walk_at(run_id, stage, column, row):
    return load_values_used(PROJECT, run_id, stage, column, row)


def test_the_sheet_holds_the_rows_the_figure_came_through(run_id):
    # Transport is G-003 (300) and G-004 (400), both east.
    values = _walk_at(run_id, "by_portfolio", "total_amount", row=1)
    # The head of the frame is G-001 and G-002, which are Health's.
    east = _step(values, "load_east")
    assert east.row_ordinals == [2, 3]
    assert east.rows == [["300"], ["400"]]


def test_a_stage_the_figure_came_through_no_row_of_says_so(run_id):
    # G-004's west copy is the one the dedupe collapsed.
    values = _walk_at(run_id, "by_portfolio", "total_amount", row=1)
    assert _step(values, "load_west").reached_rows == []
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=by_portfolio&row=1&column=total_amount")
    assert "No row of <code>load_west</code> fed this figure" in page.text


def test_the_tab_says_what_to_do_with_it(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=by_portfolio&row=1&column=total_amount")
    assert "Walk this value back to your input data" in page.text
    assert "use the arrow keys" in page.text


def test_a_step_leads_with_the_stage_description_and_says_the_mechanism(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/by_portfolio/row/1/trace/view")
    assert "Lands each grant&#39;s portfolio. AGENCY-Z matches nothing." in page.text
    assert "Combine both_regions data with load_agencies data on agency_code" in page.text
