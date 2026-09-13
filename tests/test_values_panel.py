"""The walk and the map. What a stage SHOWS is the run page's own panel."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
from app.services.workspace import resolve_run_dir
from app.web.loading import load_run_record
from app.web.scope_view import read_run_branches
from app.web.stage_diff import build_stage_diff
from app.web.values_view import build_trace_scope, load_values_used
from app.web.walk_diagram import WALK_ASIDE_FILL
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "values_fixture"
# The grand total, which five grants feed; `funded` dropped the zero-amount one.
TOTAL = ("grant_totals", "total_amount", 0)


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def _walk(run_id, stage, column, row=0):
    return load_values_used(PROJECT, run_id, stage, column, row)


def test_every_stage_the_value_came_through_is_a_step(run_id):
    # The stages between carry `amount` without writing it, and are steps all the same.
    assert _walk(run_id, "by_portfolio", "total_amount").steps == [
        "load_east", "load_west", "both_regions", "tag_portfolio", "size_band",
        "funded", "one_row_per_grant", "by_portfolio"]


def test_a_union_is_a_node_of_its_own_rather_than_two_extra_sources(run_id):
    # Contracted out, `by_portfolio` read as taking rows from load_east and load_west.
    values = _walk(run_id, "by_portfolio", "total_amount")
    assert [source.stage_id for source in values.sources["by_portfolio"]] == [
        "one_row_per_grant"]
    assert [source.stage_id for source in values.sources["both_regions"]] == [
        "load_east", "load_west"]


def _minimap_node(values, stage_id):
    return next(node for node in values.nodes if node.stage_id == stage_id)


def test_a_stage_the_value_never_touched_is_on_the_map_and_off_the_walk(run_id):
    # `mean_by_portfolio` is a sibling aggregate: a real stage, no part of this figure.
    values = _walk(run_id, "by_portfolio", "total_amount")
    aside = _minimap_node(values, "mean_by_portfolio")
    assert (aside.on_walk, aside.rows_behind) == (False, 0)
    assert _minimap_node(values, "both_regions").on_walk is True


def test_a_node_carries_the_rows_behind_the_figure(run_id):
    # Row 0 is Health's 2200: G-001, G-002 east and G-005, G-006, G-008 west.
    values = _walk(run_id, "by_portfolio", "total_amount", row=0)
    assert _minimap_node(values, "load_east").rows_behind == 2
    assert _minimap_node(values, "load_west").rows_behind == 3
    assert _minimap_node(values, "both_regions").rows_behind == 5


def _edge(values, from_stage, to_stage):
    return next(edge for edge in values.edges
                if edge.from_stage == from_stage and edge.to_stage == to_stage)


def test_a_wire_carries_the_rows_it_brought_and_says_nothing_off_the_walk(run_id):
    values = _walk(run_id, "by_portfolio", "total_amount", row=0)
    assert _edge(values, "load_east", "both_regions").rows == 2
    assert _edge(values, "load_west", "both_regions").rows == 3
    # load_agencies writes only `portfolio`, which this figure never came through.
    assert _edge(values, "load_agencies", "tag_portfolio").rows is None


def _graph_lines(values):
    return [line.strip() for line in values.mermaid.splitlines()]


def test_the_map_is_the_workflow_graph_every_other_page_draws(run_id):
    lines = _graph_lines(_walk(run_id, "by_portfolio", "portfolio"))
    assert lines[0] == "flowchart LR"
    # Off input_ids, so a union is a node with two wires rather than a contraction.
    assert "load_east -->|2| both_regions" in lines
    assert "load_west -->|3| both_regions" in lines


def test_a_stage_off_the_walk_is_greyed_and_cannot_be_opened(run_id):
    lines = _graph_lines(_walk(run_id, "by_portfolio", "total_amount"))
    assert [line for line in lines if line.startswith('click both_regions call dvNode')]
    assert not [line for line in lines if line.startswith("click mean_by_portfolio")]
    assert f"style mean_by_portfolio fill:{WALK_ASIDE_FILL}" in " ".join(lines)


def test_a_node_says_the_rows_it_holds_behind_the_figure(run_id):
    lines = _graph_lines(_walk(run_id, "by_portfolio", "total_amount", row=0))
    assert [line for line in lines if line.startswith("both_regions[")
            and "union · 5 rows behind" in line]
    assert [line for line in lines if line.startswith("mean_by_portfolio[")
            and "not on the walk" in line]


def test_a_count_reads_no_column_so_the_tab_says_so(run_id):
    assert _walk(run_id, "grant_totals", "grants").counts_rows is True


def test_the_scope_names_the_rows_of_each_stage_behind_the_figure(run_id):
    # Transport is G-003 (300) and G-004 (400), both east; west's G-004 was deduped.
    scope = build_trace_scope(PROJECT, run_id, "by_portfolio", "total_amount", row=1)
    assert scope.read_rows_at("load_east") == [2, 3]
    assert scope.read_rows_at("load_west") == []
    assert scope.cited_column == "total_amount"


def test_the_scope_says_where_each_column_was_written(run_id):
    # A column header links to the stage that wrote it, strictly upstream.
    scope = build_trace_scope(PROJECT, run_id, "by_portfolio", "portfolio", row=0)
    assert scope.column_writers["portfolio"] == "tag_portfolio"


def test_the_pane_is_the_map_and_one_empty_slot_per_stage(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        "?stage=by_portfolio&row=0&column=total_amount")
    assert page.status_code == 200
    assert page.text.count('class="vu-step"') == 8
    assert 'data-panel="load_east"' in page.text
    # No sheet of its own: every stage opens the run page's panel, fetched.
    assert 'class="data-preview"' not in page.text
    assert "flowchart LR" in page.text


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
    # Transport's two east rows, out of the six the frame holds.
    assert "2 of 6 rows behind this figure" in " ".join(page.text.split())


def test_a_traced_panel_keeps_the_run_page_tints_over_the_figures_rows(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/size_band/traced"
        "?stage=by_portfolio&row=1&column=total_amount").text
    # The stage's added columns stay blue, and the figure's rows are banded over that.
    assert "diff-col-new" in page
    assert "diff-cell-same" in page


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


def _build_filter_diff(run_id, stage_id, at_rows, rows_shown):
    record = load_run_record(PROJECT, run_id)
    output_by_id = {entry.stage_id: entry.output_path for entry in record.stage_records}
    return build_stage_diff(
        read_run_branches(PROJECT, run_id).stages[stage_id],
        resolve_run_dir(PROJECT, run_id), output_by_id[stage_id], output_by_id,
        rows_shown=rows_shown, at_rows=at_rows)


def _read_cell(diff, row, column):
    return row.cells[diff.columns.index(column)]


def test_a_scoped_filters_window_holds_the_figures_rows_and_the_rows_it_dropped(run_id):
    # size_band feeds funded in east-then-west order, so the 0-amount G-007 is input 4.
    behind = build_trace_scope(PROJECT, run_id, *TOTAL).read_rows_at("funded")
    diff = _build_filter_diff(run_id, "funded", at_rows=behind, rows_shown=6)
    assert [row.output_ordinal for row in diff.rows if not row.dropped] == behind
    assert [row.input_ordinal for row in diff.rows] == [0, 1, 3, 4, 5, 8]
    assert [_read_cell(diff, row, "grant_id") for row in diff.rows] == [
        "G-001", "G-002", "G-004", "G-007", "G-009", "G-006"]
    assert [row.dropped for row in diff.rows] == [False] * 3 + [True] + [False] * 2
    assert _read_cell(diff, diff.rows[3], "amount") == "0"
    assert diff.dropped_beyond_window == 0
    assert (diff.input_total, diff.kept_total, diff.dropped_total) == (10, 9, 1)


def test_an_unscoped_filter_still_opens_on_the_first_input_rows(run_id):
    diff = _build_filter_diff(run_id, "funded", at_rows=None, rows_shown=6)
    assert [row.input_ordinal for row in diff.rows] == [0, 1, 2, 3, 4, 5]
    assert [row.dropped for row in diff.rows] == [False] * 4 + [True, False]


def _read_traced_panel(run_id, stage_id, cited):
    stage, column, row = cited
    return " ".join(TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/{stage_id}/traced"
        f"?stage={stage}&row={row}&column={column}").text.split())


def test_the_traced_panel_of_a_filter_counts_the_figures_rows_apart_from_the_rest(run_id):
    page = _read_traced_panel(run_id, "funded", TOTAL)
    assert "showing 5 relevant input rows of 10, and 1 more drawn around them" in page
    assert "the first" not in page
    # The dropped row is drawn among them, which is what the window is widened for.
    assert "Dropped row" in page


def test_a_traced_stage_no_row_reached_says_so_rather_than_counting_to_zero(run_id):
    # Nothing behind the grand total went down to over_a_million: it dropped every row.
    page = _read_traced_panel(run_id, "over_a_million", TOTAL)
    assert "No row of this stage's output is behind this figure." in page
    assert "the first 0" not in page
    assert "relevant" not in page
    assert ">view all rows</a>" in page


def test_a_step_leads_with_the_stage_description_and_says_the_mechanism(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/by_portfolio/row/1/trace/view")
    assert "Lands each grant&#39;s portfolio. AGENCY-Z matches nothing." in page.text
    assert "Combine both_regions data with load_agencies data on agency_code" in page.text
