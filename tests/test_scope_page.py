"""The scope page: what it serves, and what it refuses. See docs/scope-map.md."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
from scope_fixture import (
    give_the_lookup_a_stage_of_its_own,
    review_tail,
    stage_specs,
    write_inputs,
)
from stage_seed import set_stages

PROJECT = "scope_fixture"
# Its own project: the run halts at the review stage, which the tests above must not.
HALTED_PROJECT = "scope_fixture_halted"
# Its own project: the lookup table it joins has a branching stage between the two.
TIERED_PROJECT = "scope_fixture_tiered"


def _execute(project: str, stages: list[dict], projects_root) -> str:
    data = projects_root / project / "data"
    write_inputs(data)
    set_stages(project, stages)
    save_working_copy_as_version(project, message="fixture")
    return str(run_service.execute(project)["run_id"])


@pytest.fixture
def run_id(projects_root):
    return _execute(PROJECT, stage_specs(projects_root / PROJECT / "data"), projects_root)


@pytest.fixture
def tiered_run_id(projects_root):
    data = projects_root / TIERED_PROJECT / "data"
    return _execute(TIERED_PROJECT,
                    give_the_lookup_a_stage_of_its_own(stage_specs(data)), projects_root)


@pytest.fixture
def halted_run_id(projects_root):
    data = projects_root / HALTED_PROJECT / "data"
    return _execute(HALTED_PROJECT, stage_specs(data) + review_tail(), projects_root)


def scope_url(project: str, run_id: str, stage: str, column: str, row: int,
              suffix: str = "") -> str:
    return (f"/project/{project}/runs/{run_id}/scope{suffix}"
            f"?stage={stage}&row={row}&column={column}")


def test_the_page_serves_the_map_the_drawing_reads(run_id):
    page = TestClient(app).get(scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0))
    assert page.status_code == 200
    assert 'id="scope-payload"' in page.text


def test_the_citation_carries_the_cell_read_back_off_the_frame(run_id):
    # The caller names a cell; printing its value means reading it, never echoing.
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    assert payload["citation"]["value"] == 2200


def test_a_row_past_the_end_of_the_frame_is_a_404(run_id):
    page = TestClient(app).get(scope_url(PROJECT, run_id, "grant_totals", "total_amount", 99))
    assert page.status_code == 404


def test_a_figure_over_two_merges_records_the_one_in_between(run_id):
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "total_of_means", "summed_means", 0,
                  suffix=".json")).json()
    assert payload["covers"]["regrained_at"] == ["total_of_means", "mean_by_portfolio"]


def test_a_figure_over_a_merge_that_no_row_fed_still_names_one_grain(run_id):
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "million_total_summed", "summed_total", 0,
                  suffix=".json")).json()
    assert payload["covers"]["at_stage"] == "million_total"
    assert payload["covers"]["ordinals"] == [0]
    assert payload["covers"]["fed_by_no_rows"] == [0]


def test_the_page_says_when_no_row_fed_the_figure(run_id):
    # 1 row of million_total is still 1 row, so counting it is not enough.
    page = TestClient(app).get(
        scope_url(PROJECT, run_id, "million_total_summed", "summed_total", 0))
    assert ("No row fed this figure: the run recorded nothing behind the 1 row it "
            "names at million_total." in page.text)


def test_the_page_says_nothing_of_the_kind_where_rows_did_feed_the_figure(run_id):
    page = TestClient(app).get(scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0))
    assert "No row fed this figure" not in page.text


def test_the_payload_carries_a_map_for_each_cut_and_none_for_an_untaken_arm(run_id):
    page = TestClient(app).get(scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0))
    payload = json.loads(re.search(
        r'<script id="scope-payload" type="application/json">(.*?)</script>',
        page.text, re.S).group(1))
    cuts = payload["cuts"]
    assert "funded|removed" in cuts
    # size_band removes nothing, so its untaken branch gets no cut to draw.
    assert not [branch for branch in cuts if branch.startswith("size_band|")]
    removed = cuts["funded|removed"]
    assert sum(removed["rows_per_branch_path"]) == removed["total"]


def test_the_json_route_serves_the_same_map(run_id):
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    assert payload["covers"]["at_stage"] == "grants_only"
    assert payload["covers"]["regrained_at"] == ["grant_totals"]
    assert len(payload["covers"]["ordinals"]) == 5


def test_the_figure_carries_its_own_row_not_only_its_contributors(run_id):
    # Clicking the figure shows the row it IS: one row of grant_totals, five behind it.
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    cited = payload["cited_row"]
    # Cells are positional against the row's own columns, as every table here is.
    by_name = dict(zip(cited["columns"], cited["cells"]))
    assert by_name["total_amount"] == 2200
    assert by_name["grants"] == 5
    assert len(payload["covers"]["ordinals"]) == 5


def test_an_unknown_stage_is_a_404(run_id):
    page = TestClient(app).get(scope_url(PROJECT, run_id, "no_such_stage", "x", 0))
    assert page.status_code == 404


def panel_url(project: str, run_id: str, stage: str, column: str, row: int = 0) -> str:
    return (f"/project/{project}/runs/{run_id}/scope/panel"
            f"?stage={stage}&row={row}&column={column}")


def test_the_panel_draws_the_same_map_without_the_project_shell(run_id):
    panel = TestClient(app).get(panel_url(PROJECT, run_id, "grant_totals", "total_amount"))
    assert panel.status_code == 200
    assert 'id="scope-payload"' in panel.text
    # The frame sits inside a page that already has a sidebar and a trail.
    assert "app-side-nav" not in panel.text


def test_the_panel_states_why_no_map_rather_than_erroring_inside_the_frame(run_id):
    panel = TestClient(app).get(panel_url(PROJECT, run_id, "no_such_stage", "x"))
    assert panel.status_code == 200
    assert "No scope map for" in panel.text


def test_a_stage_the_run_never_reached_is_left_out_of_the_map(halted_run_id):
    # It wrote no frame, so it owes no lineage sidecar: reading one is a 500.
    page = TestClient(app).get(
        scope_url(HALTED_PROJECT, halted_run_id, "grant_totals", "total_amount", 0,
                  suffix=".json"))
    assert page.status_code == 200
    assert page.json()["covers"]["at_stage"] == "grants_only"
    assert "count_reviewed" not in page.text


def test_the_row_lineage_page_draws_relevant_rows_from_the_scope_panel(run_id):
    page = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/grant_totals/row/0/trace/view")
    assert page.status_code == 200
    assert "/scope/panel?" in page.text


def test_a_cited_figure_that_is_not_a_number_carries_its_own_text(run_id):
    # scope_map.js labels the figure's node with this; formatted as a number it read NaN.
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "by_portfolio", "portfolio", 0, suffix=".json")).json()
    assert isinstance(payload["citation"]["value"], str)


def test_every_stage_the_map_draws_has_a_transform_to_show(run_id):
    # An empty Transform tab reads as "nothing happens here".
    client = TestClient(app)
    payload = client.get(scope_url(
        PROJECT, run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    blank = []
    for stage in payload["stages"]:
        panel = client.get(
            f"/project/{PROJECT}/runs/{run_id}/stage/{stage['id']}/lineage_panel?row=0")
        assert panel.status_code == 200
        if "exec-block" not in panel.text:
            blank.append(f"{stage['id']} ({stage['type']})")
    assert not blank, f"the Transform tab has nothing to show for {blank}"
def test_the_paths_pane_is_not_told_apart_by_an_aliased_merge(run_id):
    # Told apart by group, the 8 rows behind this figure are 5 paths, not 4.
    pane = TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/total_of_means/row/0/paths/panel")
    assert pane.status_code == 200
    said = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", pane.text))
    assert "4 distinct pathways reach this row" in said
    assert "merged:" not in pane.text


def test_the_aliased_merge_is_its_own_column(run_id):
    # Hung under the frame it grouped, it read as a fact about that other stage.
    payload = TestClient(app).get(scope_url(
        PROJECT, run_id, "total_of_means", "summed_means", 0, suffix=".json")).json()
    drawn = [stage["id"] for stage in payload["stages"]]
    assert "mean_by_portfolio" in drawn
    assert drawn.index("one_row_per_grant") < drawn.index("mean_by_portfolio")


def test_show_every_stage_can_reach_every_stage_on_the_route(run_id):
    payload = TestClient(app).get(scope_url(
        PROJECT, run_id, "total_of_means", "summed_means", 0, suffix=".json")).json()
    assert ([stage["id"] for stage in payload["stages"]]
            == [step["stage"] for step in payload["scale"]])

def test_the_page_carries_the_two_panes_the_script_fills(run_id):
    page = TestClient(app).get(
        scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0))
    assert 'id="scope-tabs"' in page.text
    assert 'id="scope-table"' in page.text
    assert 'id="scope-transform"' in page.text


def test_a_row_that_matched_nothing_in_a_lookup_never_came_through_it(run_id):
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    came = [payload["came_through"][index] for index in payload["came_through_index"]]
    assert all("both_regions" in stages for stages in came)
    assert any("load_agencies" not in stages for stages in came)


def test_a_lookup_table_is_named_under_the_drawing_rather_than_drawn_in_it(run_id):
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    drawn = [stage["id"] for stage in payload["stages"]]
    assert payload["lookup_tables"] == ["load_agencies"]
    assert "load_agencies" not in drawn
    assert "load_east" in drawn



def test_the_walk_crosses_the_join_and_keeps_going_up_the_lookup(tiered_run_id):
    payload = TestClient(app).get(scope_url(
        TIERED_PROJECT, tiered_run_id, "grant_totals", "total_amount", 0,
        suffix=".json")).json()
    came = [payload["came_through"][index] for index in payload["came_through_index"]]
    matched = [stages for stages in came if "load_agencies" in stages]
    assert matched, "no row of this figure matched the lookup table"
    assert all("tier_agencies" in stages for stages in matched)
    assert any("load_agencies" not in stages for stages in came)


def test_a_branch_the_lookup_took_is_on_the_path_though_no_column_draws_it(tiered_run_id):
    payload = TestClient(app).get(scope_url(
        TIERED_PROJECT, tiered_run_id, "grant_totals", "total_amount", 0,
        suffix=".json")).json()
    arms = {branch: fact["label"] for branch, fact in payload["branches"].items()
            if fact["stage_id"] == "tier_agencies"}
    assert arms, "the lookup's own branch is in no path this figure took"
    took = {branch for path in payload["branch_paths"] for branch in path}
    assert took & set(arms)
    assert "tier_agencies" not in [stage["id"] for stage in payload["stages"]]


def test_every_stage_behind_a_lookup_is_left_out_of_the_drawing(tiered_run_id):
    payload = TestClient(app).get(scope_url(
        TIERED_PROJECT, tiered_run_id, "grant_totals", "total_amount", 0,
        suffix=".json")).json()
    drawn = [stage["id"] for stage in payload["stages"]]
    assert payload["lookup_tables"] == ["load_agencies", "tier_agencies"]
    assert not set(payload["lookup_tables"]) & set(drawn)
    assert "both_regions" in drawn
