"""The Relevant columns tab. What a stage SHOWS is the run page's own panel."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
from app.web.values_view import load_values_used
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "values_fixture"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def _walk(run_id, stage, column, row=0):
    return load_values_used(PROJECT, run_id, stage, column, row)


def test_only_the_stages_that_wrote_get_a_step(run_id):
    # The six stages between pass `amount` through untouched.
    values = _walk(run_id, "by_portfolio", "total_amount")
    assert values.steps == ["load_east", "load_west", "by_portfolio"]


def test_a_union_below_a_run_of_pass_throughs_is_still_the_fork(run_id):
    values = _walk(run_id, "by_portfolio", "total_amount")
    assert [source.stage_id for source in values.sources["by_portfolio"]] == [
        "load_east", "load_west"]


def test_the_enrich_that_added_the_group_key_is_on_the_walk(run_id):
    values = _walk(run_id, "by_portfolio", "portfolio")
    assert "tag_portfolio" in values.steps


def _minimap_columns(values):
    return [[node.stage_id for node in column] for column in values.minimap]


def test_two_sources_of_one_stage_stack_rather_than_read_as_a_chain(run_id):
    # Drawn one after another they read as the first flowing into the second.
    columns = _minimap_columns(_walk(run_id, "by_portfolio", "portfolio"))
    assert set(columns[0]) == {"load_agencies", "load_east", "load_west"}
    assert columns[1:] == [["tag_portfolio"], ["by_portfolio"]]


def test_a_count_reads_no_column_so_the_tab_says_so(run_id):
    assert _walk(run_id, "grant_totals", "grants").counts_rows is True


def test_the_pane_is_the_map_and_one_empty_slot_per_step(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=by_portfolio&row=0&column=total_amount")
    assert page.status_code == 200
    assert page.text.count('class="vu-step"') == 3
    assert 'data-panel="load_east"' in page.text
    # No sheet of its own: every stage opens the run page's panel, fetched.
    assert 'class="data-preview"' not in page.text


def test_a_column_the_stage_does_not_write_is_refused_in_the_pane(run_id):
    # A pane that 404s shows the reader a browser error page inside a tab.
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=by_portfolio&row=0&column=nothing_here")
    assert page.status_code == 200
    assert "writes no column" in page.text


def test_the_tab_says_what_to_do_with_it(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=by_portfolio&row=1&column=total_amount")
    assert "Walk this value back to your input data" in page.text
    assert "use the arrow keys" in page.text


def test_a_traced_panel_is_the_run_page_panel_cut_to_the_figures_rows(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/load_east/traced"
        "?stage=by_portfolio&row=1&column=total_amount")
    assert page.status_code == 200
    # The run panel's own markup, not a second one.
    assert 'class="run-stage-panel"' in page.text
    assert '<button data-tab="schema">Schema</button>' in page.text


def test_a_scoped_panel_drops_the_run_log(run_id):
    # Nothing in the feed is per-row, so it is not offered beside filtered rows.
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/tag_portfolio/traced"
        "?stage=by_portfolio&row=1&column=total_amount")
    assert 'class="stage-run-log"' not in page.text


def test_the_run_pages_own_panel_keeps_its_log_and_its_frame_count(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/load_east/partial")
    assert 'class="stage-run-log"' in page.text
    # No figure narrows it, so nothing on it is qualified as one figure's.
    assert "behind this figure" not in page.text
    assert "for the whole stage" not in page.text


def test_a_step_leads_with_the_stage_description_and_says_the_mechanism(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/by_portfolio/row/1/trace/view")
    assert "Lands each grant&#39;s portfolio. AGENCY-Z matches nothing." in page.text
    assert "Combine both_regions data with load_agencies data on agency_code" in page.text
