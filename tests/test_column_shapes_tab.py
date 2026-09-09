"""The stage panel's Column shapes tab, over the grants workflow in scope_fixture."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.core.file_shape import ColumnKind
from app.main import app
from app.services.project import save_working_copy_as_version
from app.web.column_shapes_view import load_column_shapes
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "column_shapes_fixture"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def _shapes(run_id, at, column="total_amount", row=0):
    return load_column_shapes(PROJECT, run_id, at, cited_stage="by_portfolio",
                              cited_column=column, cited_row=row)


def _column(shapes, name, over_all=False):
    groups = shapes.over_every_row if over_all else shapes.over_relevant_rows
    return next(row for group in groups for row in group.columns if row.column == name)


def _group_of(shapes, name):
    return next(group.group.value for group in shapes.over_relevant_rows
                for row in group.columns if row.column == name)


def test_a_column_is_measured_over_the_rows_behind_the_figure_alone(run_id):
    # Row 0 is Health's 2200: G-001, G-002 east and G-005, G-006, G-008 west.
    shapes = _shapes(run_id, "both_regions")
    assert (shapes.rows_relevant, shapes.rows_in_frame) == (5, 10)
    amount = _column(shapes, "amount")
    assert amount.kind is ColumnKind.NUMBER
    assert (amount.shape.numbers.min, amount.shape.numbers.max) == (100.0, 800.0)
    assert amount.filled_count == 5


def test_the_same_frame_is_measured_over_every_row_for_the_other_basis(run_id):
    # The whole frame is ten rows: six east and four west, G-004 filed in both.
    shapes = _shapes(run_id, "both_regions")
    behind = _column(shapes, "amount")
    whole = _column(shapes, "amount", over_all=True)
    assert (behind.shape.filled_count, whole.shape.filled_count) == (5, 10)
    assert whole.shape.numbers.max == 900.0


def test_a_column_sits_under_the_heading_naming_what_the_stage_did_to_it(run_id):
    # tag_portfolio reads agency_code off both sides and lands portfolio.
    shapes = _shapes(run_id, "tag_portfolio", column="portfolio", row=0)
    assert _group_of(shapes, "agency_code") == "read"
    assert _group_of(shapes, "portfolio") == "added"
    assert _group_of(shapes, "region") == "untouched"


def test_the_groups_are_drawn_in_the_order_column_order_fixes(run_id):
    drawn = [group.group.value for group in
             _shapes(run_id, "tag_portfolio", column="portfolio", row=0).over_relevant_rows]
    assert drawn == [group for group in ["read", "changed", "added", "dropped",
                                         "untouched"] if group in drawn]


def test_the_declared_type_reaches_the_measurement(run_id):
    # tests/test_file_shape.py proves the rule on real values; this is the wiring.
    shapes = _shapes(run_id, "load_east")
    assert _column(shapes, "grant_id").shape.numbers is None


def test_a_declared_int_still_reads_as_a_number(run_id):
    assert _column(_shapes(run_id, "load_east"), "amount").kind is ColumnKind.NUMBER


def test_a_dropped_column_is_named_rather_than_measured(run_id):
    # by_portfolio rebuilds the frame: everything but its group key is gone.
    shapes = _shapes(run_id, "by_portfolio")
    dropped = next(group for group in shapes.over_relevant_rows
                   if group.group.value == "dropped")
    assert dropped.columns == []
    assert "grant_id" in dropped.dropped_names


def test_the_nulls_a_join_left_are_counted(run_id):
    # Row 2 is the group AGENCY-Z fell into: G-009 alone, matching no agency.
    shapes = _shapes(run_id, "tag_portfolio", column="portfolio", row=2)
    portfolio = _column(shapes, "portfolio")
    assert (portfolio.filled_count, portfolio.null_count) == (0, 1)
    assert portfolio.kind is ColumnKind.EMPTY


def _panel(run_id, at):
    return TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/{at}/shapes"
        "?stage=by_portfolio&row=0&column=total_amount")


def test_the_shapes_are_a_tab_of_the_shared_stage_panel(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/by_portfolio/partial")
    assert '<button data-tab="shapes">Column shapes</button>' in page.text
    # Measured on first open, like the run log below it — never with the panel.
    assert "class=\"stage-shapes\" data-shapes-href=" in page.text
    assert 'class="shape-col' not in page.text


def test_the_run_page_measures_the_whole_frame_with_no_figure_to_narrow_it(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/both_regions/shapes")
    assert page.status_code == 200
    said = " ".join(page.text.split())
    assert "all 10 rows in this frame" in said
    assert "came through" not in said


def test_the_panel_draws_the_files_page_own_column_blocks(run_id):
    page = _panel(run_id, at="both_regions")
    assert page.status_code == 200
    # _file_column.html, not a second presentation of a column shape.
    assert 'class="shape-col' in page.text
    assert 'class="facet-values"' in page.text


def test_the_panel_says_which_rows_a_figure_is_over_and_offers_the_other(run_id):
    page = _panel(run_id, at="both_regions")
    # The Input files tab's own two words, not a second vocabulary for one idea.
    assert 'data-basis="relevant">Relevant rows</button' in page.text
    assert 'data-basis="all">All rows</button' in page.text
    said = " ".join(page.text.split())
    assert "5 of the 10 rows in this frame" in said
    assert "all 10 rows in this frame" in said


def test_a_scoped_panel_names_the_rows_it_is_over(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/both_regions/traced"
        "?stage=by_portfolio&row=0&column=total_amount")
    said = " ".join(page.text.split())
    # A bare frame count beside a shapes tab measured over fewer rows read as a clash.
    assert "5 of 10 rows behind this figure" in said


def test_a_stage_no_row_reached_sends_the_reader_to_the_other_basis(run_id):
    page = _panel(run_id, at="over_a_million")
    assert "No row of <code>over_a_million</code> fed this figure" in page.text
    assert "Switch to <b>All rows</b>" in " ".join(page.text.split())


def test_an_unknown_stage_is_refused_inside_the_tab(run_id):
    page = _panel(run_id, at="nothing_here")
    assert page.status_code == 200
    assert "no stage &#39;nothing_here&#39;" in page.text
