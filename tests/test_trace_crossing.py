"""Crossing a fan-in only where the caller named the contributor to follow."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ContributorNotInFanIn
from app.main import app
from app.models import parse_stage
from app.runtime.stages.aggregate import handle_aggregate
from app.runtime.trace import ContributorChoice, trace_row, trace_to_dict
from app.services import workspace
from app.web.panel_links import AppPanelLinks
from app.web.trace_view import build_trace_view

from conftest import as_inputs, place_stage, rows_of
from test_aggregate_lineage import FILINGS
from test_trace_helpers import write_run

_VIEW = re.compile(r"^const V = (\{.*?\}), PROJECT = ", re.M | re.S)
# FILINGS' own amounts, banded at 25 — the column the row-preserving middle stage adds.
_TAGGED = FILINGS.assign(band=["small" if amt <= 25 else "large" for amt in FILINGS["amt"]])
_FIRM = {"name": "firm", "type": "str", "nullable": True}
_TOTAL = {"name": "total", "type": "int", "nullable": True}
_TAGGED_COLUMNS = [_FIRM, {"name": "amt", "type": "int", "nullable": False},
                   {"name": "band", "type": "str", "nullable": False}]


def _summing_stage():
    return parse_stage({
        "id": "agg", "type": "aggregate", "description": "agg",
        "inputs": [{"id": "tagged"}],
        "signature": {"form": "replaces",
                      "reads": [{"input": "tagged", "columns": _TAGGED_COLUMNS}],
                      "produces": [_FIRM, _TOTAL]},
        "aggregate": {"group_by": ["firm"], "aggregations": [
            {"output_column": "total", "formula": "sum", "value_column": "amt"}]},
    })


def _totalling_stage():
    return parse_stage({
        "id": "top", "type": "aggregate", "description": "top",
        "inputs": [{"id": "agg"}],
        "signature": {"form": "replaces",
                      "reads": [{"input": "agg", "columns": [_FIRM, _TOTAL]}],
                      "produces": [_TOTAL]},
        "aggregate": {"group_by": [], "aggregations": [
            {"output_column": "total", "formula": "sum", "value_column": "total"}]},
    })


def _banded_stages():
    """filings → tagged (row-preserving) → agg, so a contributor has ancestry of its own."""
    out = handle_aggregate(place_stage(_summing_stage()), as_inputs({"tagged": _TAGGED}), None)
    return [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "tagged", "type": "python_row_function", "parents": ["filings"], "df": _TAGGED},
        {"id": "agg", "type": "aggregate", "parents": ["tagged"],
         "df": rows_of(out), "lineage": out.lineage},
    ]


def _three_stage_run(tmp_path):
    stages = _banded_stages()
    return write_run(tmp_path, stages), stages[-1]["df"]


def _firm_a(totals) -> int:
    return list(totals["firm"]).index("a")


def test_no_choice_still_stops_at_the_fan_in(tmp_path):
    """The default is what stops the trace inventing a path."""
    run_dir, totals = _three_stage_run(tmp_path)

    trace = trace_row(run_dir, "agg", _firm_a(totals))

    assert [s.stage_id for s in trace.steps] == ["agg"]
    assert trace.end.reached_origin is False
    assert "summarizes its inputs" in trace.end.message
    assert trace.steps[0].followed is None


def test_a_named_contributor_carries_the_walk_into_its_own_ancestry(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)

    trace = trace_row(run_dir, "agg", _firm_a(totals), [ContributorChoice("tagged", 0)])

    # Past the crossing the walk is tagged row 0's, back to its source row.
    assert [(s.stage_id, s.row_ordinal) for s in trace.steps] == [
        ("agg", _firm_a(totals)), ("tagged", 0), ("filings", 0),
    ]
    assert trace.end.reached_origin is True
    assert trace.steps[1].row["amt"] == 10 and trace.steps[1].row["band"] == "small"
    followed = trace.steps[0].followed
    assert followed is not None
    assert (followed.stage_id, followed.row_ordinal) == ("tagged", 0)


def test_each_fan_in_met_takes_the_next_choice(tmp_path):
    """A walk can meet more than one, so the choices are a sequence applied in turn."""
    stages = _banded_stages()
    totals = stages[-1]["df"]
    stacked = handle_aggregate(
        place_stage(_totalling_stage()), as_inputs({"agg": totals}), None)
    run_dir = write_run(tmp_path, [*stages, {
        "id": "top", "type": "aggregate", "parents": ["agg"],
        "df": rows_of(stacked), "lineage": stacked.lineage}])

    trace = trace_row(run_dir, "top", 0, [
        ContributorChoice("agg", _firm_a(totals)), ContributorChoice("tagged", 0)])

    assert [(s.stage_id, s.row_ordinal) for s in trace.steps] == [
        ("top", 0), ("agg", _firm_a(totals)), ("tagged", 0), ("filings", 0),
    ]
    assert trace.end.reached_origin is True


def test_one_choice_crosses_the_first_fan_in_and_stops_at_the_second(tmp_path):
    stages = _banded_stages()
    totals = stages[-1]["df"]
    stacked = handle_aggregate(
        place_stage(_totalling_stage()), as_inputs({"agg": totals}), None)
    run_dir = write_run(tmp_path, [*stages, {
        "id": "top", "type": "aggregate", "parents": ["agg"],
        "df": rows_of(stacked), "lineage": stacked.lineage}])

    trace = trace_row(run_dir, "top", 0, [ContributorChoice("agg", _firm_a(totals))])

    assert [s.stage_id for s in trace.steps] == ["top", "agg"]
    assert trace.end.at_stage == "agg" and trace.end.reached_origin is False


def test_a_choice_naming_a_row_that_fed_nothing_here_fails_loudly(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)

    # tagged row 1 has no firm, so it fed the missing-key group, not firm "a".
    with pytest.raises(ContributorNotInFanIn) as caught:
        trace_row(run_dir, "agg", _firm_a(totals), [ContributorChoice("tagged", 1)])

    assert "'tagged' row 1" in str(caught.value)


def test_a_choice_naming_a_stage_that_fed_nothing_fails_loudly(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)

    with pytest.raises(ContributorNotInFanIn):
        trace_row(run_dir, "agg", _firm_a(totals), [ContributorChoice("filings", 0)])


def test_a_choice_the_walk_never_met_fails_loudly(tmp_path):
    """Dropping it would show a shorter path than the caller asked for."""
    run_dir, _ = _three_stage_run(tmp_path)

    with pytest.raises(ContributorNotInFanIn) as caught:
        trace_row(run_dir, "tagged", 0, [ContributorChoice("filings", 0)])

    assert "met no fan-in" in str(caught.value)


def test_the_crossed_step_is_compared_against_nothing(tmp_path):
    """The row below a crossing is a contributor's, so a diff would invent a transform."""
    run_dir, totals = _three_stage_run(tmp_path)
    trace = trace_to_dict(
        trace_row(run_dir, "agg", _firm_a(totals), [ContributorChoice("tagged", 0)]))

    view = build_trace_view(trace, {}, AppPanelLinks("proj", "T1"))

    crossed = view["nodes"][-1]
    assert crossed["stage_id"] == "agg"
    assert crossed["base"] is None
    assert crossed["row_diff"]["changed"] == 0 and crossed["row_diff"]["added"] == 0
    assert crossed["followed"]["stage_id"] == "tagged"
    assert crossed["followed"]["row_ordinal"] == 0
    assert crossed["followed"]["of"] == 2, "firm 'a' was totalled from two filings"


def test_the_step_below_a_crossing_still_compares_against_its_own_parent(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)
    trace = trace_to_dict(
        trace_row(run_dir, "agg", _firm_a(totals), [ContributorChoice("tagged", 0)]))

    view = build_trace_view(trace, {}, AppPanelLinks("proj", "T1"))

    tagged = view["nodes"][1]
    assert tagged["base"] == {"stage_id": "filings", "row_ordinal": 0}
    assert tagged["row_diff"]["added"] == 1, "the band column"
    assert tagged["followed"] is None


def test_the_followed_row_is_named_once_in_the_stories(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)
    firm_a = _firm_a(totals)
    trace = trace_to_dict(
        trace_row(run_dir, "agg", firm_a, [ContributorChoice("tagged", 0)]))

    stories = build_trace_view(trace, {}, AppPanelLinks("proj", "T1"))["stories"]

    # The picked row reads as followed; the cohort's other row is still offered.
    assert [s["kind"] for s in stories] == ["shown", "crossed", "contributor"]
    assert (stories[1]["stage_id"], stories[1]["row_ordinal"]) == ("tagged", 0)
    assert stories[1]["href"] is None
    assert stories[2]["href"] == (
        f"/project/proj/runs/T1/stage/agg/row/{firm_a}/trace/view?via=tagged%3A2")


def _serve(tmp_path, project: str):
    workspace.set_projects_dir(tmp_path)
    run_dir, totals = _three_stage_run(tmp_path / project / "runs")
    return project, run_dir.name, totals


def _embedded_view(page: str) -> dict:
    blob = _VIEW.search(page)
    assert blob, "the page must embed its view model"
    return json.loads(blob.group(1))


def test_the_route_crosses_where_via_names_a_contributor(tmp_path):
    project, run_id, totals = _serve(tmp_path, "crossing_route")
    base = f"/project/{project}/runs/{run_id}/stage/agg/row/{_firm_a(totals)}/trace/view"

    view = _embedded_view(TestClient(app).get(base, params={"via": "tagged:0"}).text)

    assert [n["stage_id"] for n in view["nodes"]] == ["filings", "tagged", "agg"]
    assert view["nodes"][-1]["followed"]["row_ordinal"] == 0


def test_the_route_refuses_a_via_that_names_no_contributor(tmp_path):
    project, run_id, totals = _serve(tmp_path, "crossing_route_refusal")
    base = f"/project/{project}/runs/{run_id}/stage/agg/row/{_firm_a(totals)}/trace/view"
    client = TestClient(app)

    assert client.get(base, params={"via": "filings:0"}).status_code == 400
    assert client.get(base, params={"via": "tagged"}).status_code == 400
    # Without one the page still renders, stopped at the fan-in.
    assert client.get(base).status_code == 200


def test_the_cohort_table_carries_the_page_that_sent_the_reader(tmp_path):
    project, run_id, totals = _serve(tmp_path, "crossing_route_cohort")
    firm_a = _firm_a(totals)
    trace = trace_to_dict(trace_row(
        workspace.projects_dir() / project / "runs" / run_id, "agg", firm_a))

    view = build_trace_view(trace, {}, AppPanelLinks(project, run_id))

    rows_link = view["nodes"][0]["contributor_groups"][0]["rows_link"]
    assert f"owner=agg%3A{firm_a}" in rows_link
    page = TestClient(app).get(rows_link)
    assert page.status_code == 200
    # Each listed row offers the crossing, not a walk that starts over here.
    assert f"/stage/agg/row/{firm_a}/trace/view?via=tagged%3A0" in page.text
    assert "Follow this row" in page.text


def test_a_rows_table_reached_without_an_owner_keeps_its_own_trace_links(tmp_path):
    project, run_id, _ = _serve(tmp_path, "crossing_route_plain")

    page = TestClient(app).get(f"/project/{project}/runs/{run_id}/stage/tagged/rows")

    assert page.status_code == 200
    assert f"/project/{project}/runs/{run_id}/stage/tagged/row/0/trace/view" in page.text
    assert "Follow this row" not in page.text


def test_the_json_route_carries_the_crossing(tmp_path):
    project, run_id, totals = _serve(tmp_path, "crossing_route_json")
    url = f"/project/{project}/runs/{run_id}/stage/agg/row/{_firm_a(totals)}/trace"

    payload = TestClient(app).get(url, params={"via": "tagged:0"}).json()

    assert [s["stage_id"] for s in payload["steps"]] == ["agg", "tagged", "filings"]
    assert payload["steps"][0]["followed"]["row_ordinal"] == 0
    assert payload["steps"][1]["followed"] is None
