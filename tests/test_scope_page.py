"""The scope page: what it serves, and what it refuses. See docs/scope-map.md."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "scope_fixture"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture", reviewer="test")
    return str(run_service.execute(PROJECT)["run_id"])


def scope_url(run_id: str, stage: str, column: str, row: int, suffix: str = "") -> str:
    return (f"/project/{PROJECT}/runs/{run_id}/scope{suffix}"
            f"?stage={stage}&row={row}&column={column}")


def test_the_page_names_the_figure_and_what_its_rows_establish(run_id):
    page = TestClient(app).get(scope_url(run_id, "grant_totals", "total_amount", 0))
    assert page.status_code == 200
    assert "grant_totals" in page.text
    assert "Computed from 5 rows of grants_only" in page.text


def test_the_citation_carries_the_cell_read_back_off_the_frame(run_id):
    # The caller names a cell; printing its value means reading it, never echoing.
    payload = TestClient(app).get(
        scope_url(run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    assert payload["citation"]["value"] == 2200


def test_a_row_past_the_end_of_the_frame_is_a_404(run_id):
    page = TestClient(app).get(scope_url(run_id, "grant_totals", "total_amount", 99))
    assert page.status_code == 404


def test_a_figure_over_two_merges_names_the_one_in_between(run_id):
    page = TestClient(app).get(scope_url(run_id, "total_of_means", "summed_means", 0))
    assert page.status_code == 200
    assert "merged at mean_by_portfolio before this figure was taken" in page.text


def test_a_figure_over_a_merge_that_no_row_fed_still_names_one_grain(run_id):
    payload = TestClient(app).get(
        scope_url(run_id, "million_total_summed", "summed_total", 0,
                  suffix=".json")).json()
    assert payload["covers"]["at_stage"] == "million_total"
    assert payload["covers"]["ordinals"] == [0]
    assert payload["covers"]["fed_by_no_rows"] == [0]


def test_the_page_says_when_no_row_fed_the_figure(run_id):
    # 1 row of million_total is still 1 row, so counting it is not enough.
    page = TestClient(app).get(
        scope_url(run_id, "million_total_summed", "summed_total", 0))
    assert ("No row fed this figure: the run recorded nothing behind the 1 row it "
            "names at million_total." in page.text)


def test_the_page_says_nothing_of_the_kind_where_rows_did_feed_the_figure(run_id):
    page = TestClient(app).get(scope_url(run_id, "grant_totals", "total_amount", 0))
    assert "No row fed this figure" not in page.text


def test_the_page_says_how_much_of_the_widest_frame_is_off_screen(run_id):
    page = TestClient(app).get(scope_url(run_id, "grant_totals", "total_amount", 0))
    assert "of the 10 rows at both_regions" in page.text
    assert "The rest of that frame is not drawn" in page.text


def test_the_payload_carries_a_map_for_each_cut_and_none_for_an_untaken_arm(run_id):
    page = TestClient(app).get(scope_url(run_id, "grant_totals", "total_amount", 0))
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
        scope_url(run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    assert payload["covers"]["at_stage"] == "grants_only"
    assert payload["covers"]["regrained_at"] == ["grant_totals"]
    assert len(payload["covers"]["ordinals"]) == 5


def test_the_figure_carries_its_own_row_not_only_its_contributors(run_id):
    # Clicking the figure shows the row it IS: one row of grant_totals, five behind it.
    payload = TestClient(app).get(
        scope_url(run_id, "grant_totals", "total_amount", 0, suffix=".json")).json()
    cited = payload["cited_row"]
    # Cells are positional against the row's own columns, as every table here is.
    by_name = dict(zip(cited["columns"], cited["cells"]))
    assert by_name["total_amount"] == 2200
    assert by_name["grants"] == 5
    assert len(payload["covers"]["ordinals"]) == 5


def test_an_unknown_stage_is_a_404(run_id):
    page = TestClient(app).get(scope_url(run_id, "no_such_stage", "x", 0))
    assert page.status_code == 404
