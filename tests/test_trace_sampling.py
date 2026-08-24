"""Sampling at a fan-in: the first recorded row, marked wherever a choice existed."""
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
    """Two of FILINGS' rows carry firm "a", so its group is a fan-in with a choice."""
    return list(totals["firm"]).index("a")


def _firm_b(totals) -> int:
    """Only FILINGS row 4 carries firm "b", so its group is a fan-in of exactly one."""
    return list(totals["firm"]).index("b")


def test_with_nothing_supplied_the_walk_samples_its_way_to_the_input_data(tmp_path):
    """The default reaches the source; the mark is what says a row was sampled."""
    run_dir, totals = _three_stage_run(tmp_path)

    trace = trace_row(run_dir, "agg", _firm_a(totals))

    assert [(s.stage_id, s.row_ordinal) for s in trace.steps] == [
        ("agg", _firm_a(totals)), ("tagged", 0), ("filings", 0),
    ]
    assert trace.end.reached_origin is True
    # firm "a" was totalled from tagged rows 0 and 2; the FIRST recorded is taken.
    assert trace.steps[0].followed.row_ordinal == 0
    assert [s.followed for s in trace.steps[1:]] == [None, None]


def test_a_row_no_step_summarized_samples_nothing_at_all(tmp_path):
    """The invariant: nothing marked means row-level lineage the whole way."""
    run_dir, _ = _three_stage_run(tmp_path)

    trace = trace_row(run_dir, "tagged", 3)

    assert [(s.stage_id, s.row_ordinal) for s in trace.steps] == [
        ("tagged", 3), ("filings", 3),
    ]
    assert [s.followed for s in trace.steps] == [None, None]
    assert trace.end.reached_origin is True


def test_a_named_contributor_overrides_which_row_is_sampled(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)

    trace = trace_row(run_dir, "agg", _firm_a(totals), [ContributorChoice("tagged", 2)])

    # Row 2, not the row 0 the default would have taken.
    assert [(s.stage_id, s.row_ordinal) for s in trace.steps] == [
        ("agg", _firm_a(totals)), ("tagged", 2), ("filings", 2),
    ]
    assert trace.end.reached_origin is True
    assert trace.steps[1].row["amt"] == 30 and trace.steps[1].row["band"] == "large"
    followed = trace.steps[0].followed
    assert followed is not None
    assert (followed.stage_id, followed.row_ordinal) == ("tagged", 2)


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


def test_one_choice_overrides_its_fan_in_and_the_rest_are_sampled_by_default(tmp_path):
    stages = _banded_stages()
    totals = stages[-1]["df"]
    stacked = handle_aggregate(
        place_stage(_totalling_stage()), as_inputs({"agg": totals}), None)
    run_dir = write_run(tmp_path, [*stages, {
        "id": "top", "type": "aggregate", "parents": ["agg"],
        "df": rows_of(stacked), "lineage": stacked.lineage}])

    trace = trace_row(run_dir, "top", 0, [ContributorChoice("agg", _firm_a(totals))])

    # The choice steers `top`; `agg`'s own fan-in takes its first edge unaided.
    assert [(s.stage_id, s.row_ordinal) for s in trace.steps] == [
        ("top", 0), ("agg", _firm_a(totals)), ("tagged", 0), ("filings", 0),
    ]
    assert trace.end.reached_origin is True


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


def test_a_fan_in_of_one_row_marks_nothing_at_all(tmp_path):
    """Nothing was sampled there, so a caution would teach the reader to ignore cautions."""
    run_dir, totals = _three_stage_run(tmp_path)
    trace = trace_row(run_dir, "agg", _firm_b(totals))

    view = build_trace_view(trace_to_dict(trace), {}, AppPanelLinks("proj", "T1"))

    assert [(s.stage_id, s.row_ordinal) for s in trace.steps] == [
        ("agg", _firm_b(totals)), ("tagged", 4), ("filings", 4),
    ]
    assert trace.steps[0].followed.row_ordinal == 4, "the walk still took it"
    assert [n["sampled"] for n in view["nodes"]] == [None, None, None]
    # Nor is it offered in the pane: there was no alternative to offer.
    assert [s["kind"] for s in view["stories"]] == ["shown"]


def test_an_unmarked_one_row_fan_in_is_still_not_compared_across(tmp_path):
    """An aggregate row is a summary, not its one contributor carried forward."""
    run_dir, totals = _three_stage_run(tmp_path)

    view = build_trace_view(
        trace_to_dict(trace_row(run_dir, "agg", _firm_b(totals))),
        {}, AppPanelLinks("proj", "T1"))

    summary = view["nodes"][-1]
    assert summary["stage_id"] == "agg"
    assert summary["base"] is None
    assert summary["row_diff"]["changed"] == 0 and summary["row_diff"]["added"] == 0


def test_the_mark_says_which_row_of_how_many_was_sampled(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)

    view = build_trace_view(
        trace_to_dict(trace_row(run_dir, "agg", _firm_a(totals))),
        {}, AppPanelLinks("proj", "T1"))

    # amt=10 and amt=30 were totalled into firm "a", and the mark says so.
    assert view["nodes"][-1]["sampled"]["of"] == 2
    # The PLACE among contributors, not the ordinal at `tagged`.
    assert view["nodes"][-1]["sampled"]["at"] == 1
    assert view["nodes"][-1]["sampled"]["row_ordinal"] == 0
    assert [s["rows"] for s in view["stories"] if s["kind"] == "sampled"] == [2]


def test_an_override_moves_the_place_but_not_the_count(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)

    view = build_trace_view(
        trace_to_dict(trace_row(run_dir, "agg", _firm_a(totals),
                                [ContributorChoice("tagged", 2)])),
        {}, AppPanelLinks("proj", "T1"))

    # Second of firm "a"'s two contributors, and separately row 2 at `tagged`.
    assert view["nodes"][-1]["sampled"]["at"] == 2
    assert view["nodes"][-1]["sampled"]["of"] == 2
    assert view["nodes"][-1]["sampled"]["row_ordinal"] == 2


def test_the_sampling_step_is_compared_against_nothing(tmp_path):
    """The row below a sample is a contributor's, so a diff would invent a transform."""
    run_dir, totals = _three_stage_run(tmp_path)
    trace = trace_to_dict(
        trace_row(run_dir, "agg", _firm_a(totals), [ContributorChoice("tagged", 0)]))

    view = build_trace_view(trace, {}, AppPanelLinks("proj", "T1"))

    summary = view["nodes"][-1]
    assert summary["stage_id"] == "agg"
    assert summary["base"] is None
    assert summary["row_diff"]["changed"] == 0 and summary["row_diff"]["added"] == 0
    assert summary["sampled"]["stage_id"] == "tagged"
    assert summary["sampled"]["row_ordinal"] == 0
    assert summary["sampled"]["of"] == 2, "firm 'a' was totalled from two filings"


def test_the_step_below_a_sampled_row_still_compares_against_its_own_parent(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)
    trace = trace_to_dict(
        trace_row(run_dir, "agg", _firm_a(totals), [ContributorChoice("tagged", 0)]))

    view = build_trace_view(trace, {}, AppPanelLinks("proj", "T1"))

    tagged = view["nodes"][1]
    assert tagged["base"] == {"stage_id": "filings", "row_ordinal": 0}
    assert tagged["row_diff"]["added"] == 1, "the band column"
    assert tagged["sampled"] is None


def test_the_sampled_row_is_named_once_in_the_stories(tmp_path):
    run_dir, totals = _three_stage_run(tmp_path)
    firm_a = _firm_a(totals)
    trace = trace_to_dict(
        trace_row(run_dir, "agg", firm_a, [ContributorChoice("tagged", 0)]))

    stories = build_trace_view(trace, {}, AppPanelLinks("proj", "T1"))["stories"]

    # The picked row reads as followed; the cohort's other row is still offered.
    assert [s["kind"] for s in stories] == ["shown", "sampled", "contributor"]
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


def test_the_route_samples_with_no_via_at_all(tmp_path):
    project, run_id, totals = _serve(tmp_path, "sampling_route_default")
    base = f"/project/{project}/runs/{run_id}/stage/agg/row/{_firm_a(totals)}/trace/view"

    view = _embedded_view(TestClient(app).get(base).text)

    assert [n["stage_id"] for n in view["nodes"]] == ["filings", "tagged", "agg"]
    assert view["upstream"]["truncated"] is False
    assert view["nodes"][-1]["sampled"]["row_ordinal"] == 0


def test_the_route_lets_via_swap_which_row_is_sampled(tmp_path):
    project, run_id, totals = _serve(tmp_path, "sampling_route")
    base = f"/project/{project}/runs/{run_id}/stage/agg/row/{_firm_a(totals)}/trace/view"

    view = _embedded_view(TestClient(app).get(base, params={"via": "tagged:2"}).text)

    assert [(n["stage_id"], n["row_ordinal"]) for n in view["nodes"]] == [
        ("filings", 2), ("tagged", 2), ("agg", _firm_a(totals)),
    ]
    assert view["nodes"][-1]["sampled"]["row_ordinal"] == 2


def test_a_mark_is_drawn_at_every_fan_in_that_held_a_choice(tmp_path):
    """The page invariant: a mark is a place where a row was picked out of several."""
    stages = _banded_stages()
    totals = stages[-1]["df"]
    stacked = handle_aggregate(
        place_stage(_totalling_stage()), as_inputs({"agg": totals}), None)
    run_dir = write_run(tmp_path, [*stages, {
        "id": "top", "type": "aggregate", "parents": ["agg"],
        "df": rows_of(stacked), "lineage": stacked.lineage}])

    marked = build_trace_view(
        trace_to_dict(trace_row(run_dir, "top", 0)), {}, AppPanelLinks("proj", "T1"))
    unbroken = build_trace_view(
        trace_to_dict(trace_row(run_dir, "tagged", 3)), {}, AppPanelLinks("proj", "T1"))

    # Two aggregates on the path, each merging more than one row, so two marks.
    assert sum(1 for n in marked["nodes"] if n["sampled"]) == 2
    assert [n["stage_id"] for n in marked["nodes"]] == [
        "filings", "tagged", "agg", "top"]
    # No aggregate on this one, so nothing was picked and nothing is marked.
    assert [n["sampled"] for n in unbroken["nodes"]] == [None, None]
    assert unbroken["upstream"]["truncated"] is False


def test_the_route_refuses_a_via_that_names_no_contributor(tmp_path):
    project, run_id, totals = _serve(tmp_path, "sampling_route_refusal")
    base = f"/project/{project}/runs/{run_id}/stage/agg/row/{_firm_a(totals)}/trace/view"
    client = TestClient(app)

    assert client.get(base, params={"via": "filings:0"}).status_code == 400
    assert client.get(base, params={"via": "tagged"}).status_code == 400
    # Without one the page renders the default sample rather than refusing.
    assert client.get(base).status_code == 200


def test_the_cohort_table_carries_the_page_that_sent_the_reader(tmp_path):
    project, run_id, totals = _serve(tmp_path, "sampling_route_cohort")
    firm_a = _firm_a(totals)
    trace = trace_to_dict(trace_row(
        workspace.projects_dir() / project / "runs" / run_id, "agg", firm_a))

    view = build_trace_view(trace, {}, AppPanelLinks(project, run_id))

    rows_link = view["nodes"][-1]["contributor_groups"][0]["rows_link"]
    assert f"owner=agg%3A{firm_a}" in rows_link
    page = TestClient(app).get(rows_link)
    assert page.status_code == 200
    # Each listed row offers the override, not a walk that starts over here.
    assert f"/stage/agg/row/{firm_a}/trace/view?via=tagged%3A2" in page.text
    assert "Follow this row" in page.text


def test_a_rows_table_reached_without_an_owner_keeps_its_own_trace_links(tmp_path):
    project, run_id, _ = _serve(tmp_path, "sampling_route_plain")

    page = TestClient(app).get(f"/project/{project}/runs/{run_id}/stage/tagged/rows")

    assert page.status_code == 200
    assert f"/project/{project}/runs/{run_id}/stage/tagged/row/0/trace/view" in page.text
    assert "Follow this row" not in page.text


def test_the_json_route_carries_the_sampled_edge(tmp_path):
    project, run_id, totals = _serve(tmp_path, "sampling_route_json")
    url = f"/project/{project}/runs/{run_id}/stage/agg/row/{_firm_a(totals)}/trace"

    payload = TestClient(app).get(url, params={"via": "tagged:0"}).json()

    # `followed` is the runtime's edge at EVERY fan-in, not the view's mark.
    assert [s["stage_id"] for s in payload["steps"]] == ["agg", "tagged", "filings"]
    assert payload["steps"][0]["followed"]["row_ordinal"] == 0
    assert payload["steps"][1]["followed"] is None
