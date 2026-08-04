"""Tracing across a merge: the walk follows the subject spine and reports the
reference side as a branch the reader can promote into a trace of its own."""
from __future__ import annotations

import pandas as pd

from app.models import parse_stage
from app.runtime.lineage import (
    EdgeKind,
    RowLineage,
    RowParent,
    read_row_lineage,
)
from app.runtime.stages.join import handle_enrich, handle_expand
from app.runtime.trace import trace_row
from test_trace_helpers import write_run

FILINGS = pd.DataFrame({"client": ["Acme", "Borealis"], "amount": [500, 1200]})
CONTRACTS = pd.DataFrame({"client": ["Acme"], "agency": ["HHS"]})
# An enrich of the two: Acme matched, Borealis did not.
JOINED = pd.DataFrame({
    "client": ["Acme", "Borealis"],
    "amount": [500, 1200],
    "agency": ["HHS", None],
})


def _join_run(tmp_path):
    # filings + contracts -> j (enrich). Row 0 matched both sides; row 1 is the
    # unmatched subject row, so it has one parent, not two.
    lineage = RowLineage([
        [RowParent("filings", 0), RowParent("contracts", 0)],
        [RowParent("filings", 1)],
    ])
    return write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "contracts", "type": "input_data", "parents": [], "df": CONTRACTS},
        {"id": "j", "type": "enrich", "parents": ["filings", "contracts"],
         "df": JOINED, "lineage": lineage},
    ])


def test_merge_walk_follows_the_subject_spine_to_origin(tmp_path):
    # Before this, a merge stopped the walk dead with the issue-58 message.
    trace = trace_row(_join_run(tmp_path), "j", 0)
    assert [s.stage_id for s in trace.steps] == ["j", "filings"]
    assert [s.row_ordinal for s in trace.steps] == [0, 0]
    assert trace.end.reached_origin is True


def test_the_unfollowed_side_is_reported_as_a_branch(tmp_path):
    trace = trace_row(_join_run(tmp_path), "j", 0)
    assert trace.steps[0].branches == [RowParent("contracts", 0, EdgeKind.direct.value)]
    # The spine step itself carries no branch — it had a single parent.
    assert trace.steps[1].branches == []


def test_promoting_a_branch_is_just_another_trace(tmp_path):
    # The view re-enters trace_row at the branch: no queue, no pre-expansion —
    # one walk per branch actually opened.
    run_dir = _join_run(tmp_path)
    branch = trace_row(run_dir, "j", 0).steps[0].branches[0]
    promoted = trace_row(run_dir, branch.stage_id, branch.row_ordinal)
    assert [s.stage_id for s in promoted.steps] == ["contracts"]
    assert promoted.steps[0].row["agency"] == "HHS"
    assert promoted.end.reached_origin is True


def test_an_unmatched_row_has_one_parent_and_no_branch(tmp_path):
    # The recorded non-match: Borealis' agency is null whether no row matched or
    # a matched row's column was itself null. Only the absent parent tells those apart.
    trace = trace_row(_join_run(tmp_path), "j", 1)
    assert [s.stage_id for s in trace.steps] == ["j", "filings"]
    assert trace.steps[0].branches == []
    assert trace.steps[0].row["agency"] is None
    assert trace.end.reached_origin is True


def test_spine_follows_the_right_side_when_only_it_matched(tmp_path):
    # An unmatched RIGHT row has the right side as its only parent, so the spine
    # follows the data rather than a left-side default.
    lineage = RowLineage([[RowParent("contracts", 0)]])
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "contracts", "type": "input_data", "parents": [], "df": CONTRACTS},
        {"id": "j", "type": "enrich", "parents": ["filings", "contracts"],
         "df": pd.DataFrame({"client": ["Acme"], "amount": [None], "agency": ["HHS"]}),
         "lineage": lineage},
    ])
    trace = trace_row(run_dir, "j", 0)
    assert [s.stage_id for s in trace.steps] == ["j", "contracts"]
    assert trace.steps[0].branches == []
    assert trace.end.reached_origin is True


def test_contribution_parents_are_never_walked_into(tmp_path):
    # An aggregate's contributors are a cohort to open, not a step to take, so the
    # walk ends at the stage with them reported as branches.
    lineage = RowLineage([[
        RowParent("filings", 0, EdgeKind.contribution.value),
        RowParent("filings", 1, EdgeKind.contribution.value),
    ]])
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "agg", "type": "aggregate", "parents": ["filings"],
         "df": pd.DataFrame({"total": [1700]}), "lineage": lineage},
    ])
    trace = trace_row(run_dir, "agg", 0)
    assert [s.stage_id for s in trace.steps] == ["agg"]
    assert len(trace.steps[0].branches) == 2
    assert trace.end.reached_origin is False
    assert "summarizes" in trace.end.message
    # Each contributor is still a promotable starting point.
    promoted = trace_row(run_dir, "filings", trace.steps[0].branches[1].row_ordinal)
    assert promoted.steps[0].row["client"] == "Borealis"


def test_branches_survive_serialization(tmp_path):
    from app.runtime.trace import trace_to_dict
    payload = trace_to_dict(trace_row(_join_run(tmp_path), "j", 0))
    assert payload["steps"][0]["branches"] == [
        {"stage_id": "contracts", "row_ordinal": 0, "kind": "direct"}
    ]


def test_branches_reach_the_render_payload(tmp_path):
    # The view offers a branch as a promotable trace; the template turns it into a
    # link back into this same page at that row.
    from app.runtime.trace import trace_to_dict
    from app.runtime.trace_view import build_trace_view
    view = build_trace_view(trace_to_dict(trace_row(_join_run(tmp_path), "j", 0)), {})
    by_stage = {n["stage_id"]: n for n in view["nodes"]}
    assert by_stage["j"]["branches"] == [
        {"stage_id": "contracts", "row_ordinal": 0, "kind": "direct"}
    ]
    assert by_stage["filings"]["branches"] == []


def test_handler_lineage_reaches_the_executor_channel():
    # The seam: what the handler attaches is what the executor persists. Guards the
    # `.attrs` channel against a later rebuild of the frame dropping it silently —
    # the projection and the temp-column drop both rebuild.
    stage = parse_stage({
        "id": "j", "type": "enrich", "name": "j",
        "inputs": [
            {"id": "filings", "schema": {"columns": [
                {"name": "client", "type": "str", "nullable": False},
                {"name": "amount", "type": "int", "nullable": False}]}},
            {"id": "contracts", "schema": {"columns": [
                {"name": "client", "type": "str", "nullable": False},
                {"name": "agency", "type": "str", "nullable": False}]}},
        ],
        "output_schema": {"columns": [{"name": "client", "type": "str", "nullable": False}]},
        "join": {"keys": [{"left": "client", "right": "client"}],
                  "bring": ["agency"]},
    })
    out = handle_enrich(stage, {"filings": FILINGS, "contracts": CONTRACTS}, None)
    lineage = read_row_lineage(out)
    assert lineage is not None
    assert len(lineage) == len(out)
    assert lineage.parents == [
        [RowParent("filings", 0), RowParent("contracts", 0)],
        [RowParent("filings", 1)],
    ]
    # The ordinal carriers never reach the persisted frame.
    assert not [c for c in out.columns if c.startswith("_trace")]


def test_expand_records_the_subject_row_each_fanned_out_row_came_from():
    # expand is where recording is the ONLY route: m:n means output row i need not
    # be subject row i, so nothing positional can recover the pairing afterwards.
    stage = parse_stage({
        "id": "x", "type": "expand", "name": "x",
        "inputs": [
            {"id": "filings", "schema": {"columns": [
                {"name": "client", "type": "str", "nullable": False},
                {"name": "amount", "type": "int", "nullable": False}]}},
            {"id": "contracts", "schema": {"columns": [
                {"name": "client", "type": "str", "nullable": False},
                {"name": "agency", "type": "str", "nullable": False}]}},
        ],
        "output_schema": {"columns": [
            {"name": "client", "type": "str", "nullable": False},
            {"name": "agency", "type": "str", "nullable": True}]},
        "join": {"keys": [{"left": "client", "right": "client"}],
                 "bring": ["agency"]},
    })
    two_contracts = pd.DataFrame({"client": ["Acme", "Acme"], "agency": ["HHS", "DOD"]})

    out = handle_expand(stage, {"filings": FILINGS, "contracts": two_contracts}, None)

    assert list(out["agency"])[:2] == ["HHS", "DOD"]
    assert pd.isna(out["agency"].iat[2])
    lineage = read_row_lineage(out)
    assert lineage is not None
    # Both fanned-out rows name the SAME subject row; the unmatched one still has
    # a single parent, so the fan-out and the non-match are both readable.
    assert lineage.parents == [
        [RowParent("filings", 0), RowParent("contracts", 0)],
        [RowParent("filings", 0), RowParent("contracts", 1)],
        [RowParent("filings", 1)],
    ]


# ── without a sidecar there is nothing to cross ──────────────────────────────
# A run recorded before the runtime captured join lineage stops at the join, and
# re-running the workflow is what makes it traceable. An enrich's output IS in
# subject order, so crossing it on ordinal alone would also work (PR #348 showed
# this) — but that is a second, invisible route to an answer the sidecar already
# gives, and a reader cannot tell which one produced the trace they are reading.


def _join_run_without_sidecar(tmp_path, stage_type):
    return write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "contracts", "type": "input_data", "parents": [], "df": CONTRACTS},
        {"id": "j", "type": stage_type, "parents": ["filings", "contracts"], "df": JOINED},
    ], run_id=f"T_{stage_type}")


def test_a_join_without_a_sidecar_stops_the_walk(tmp_path):
    for stage_type in ("enrich", "expand"):
        trace = trace_row(_join_run_without_sidecar(tmp_path, stage_type), "j", 0)
        assert [s.stage_id for s in trace.steps] == ["j"]
        assert trace.end.reached_origin is False
        assert "issue #58" in trace.end.message


def test_columns_new_is_only_what_the_join_added(tmp_path):
    # The spine names the frame to diff against. With no parent to diff against a
    # join reports every column it carries, which overstates its contribution —
    # so this is a property of the recorded run, not of the stage type.
    trace = trace_row(_join_run(tmp_path), "j", 0)
    assert trace.steps[0].columns_new == ["agency"]
    assert trace_row(
        _join_run_without_sidecar(tmp_path, "enrich"), "j", 0
    ).steps[0].columns_new == ["client", "amount", "agency"]
