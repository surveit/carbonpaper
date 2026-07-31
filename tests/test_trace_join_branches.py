"""Tracing across a merge: the walk follows the subject spine and reports the
reference side as a branch the reader can promote into a trace of its own."""
from __future__ import annotations

import pandas as pd

from app.models import Stage
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
    """filings + contracts -> j (enrich). Row 0 matched both sides; row 1 is the
    unmatched subject row, so it has one parent, not two."""
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
    assert trace.steps[0].branches == [RowParent("contracts", 0, EdgeKind.derivation.value)]
    # The spine step itself carries no branch — it had a single parent.
    assert trace.steps[1].branches == []


def test_promoting_a_branch_is_just_another_trace(tmp_path):
    """The reader clicks the branch chip; the view re-enters trace_row at it.
    No queue, no pre-expansion — one walk per branch actually opened."""
    run_dir = _join_run(tmp_path)
    branch = trace_row(run_dir, "j", 0).steps[0].branches[0]
    promoted = trace_row(run_dir, branch.stage_id, branch.row_ordinal)
    assert [s.stage_id for s in promoted.steps] == ["contracts"]
    assert promoted.steps[0].row["agency"] == "HHS"
    assert promoted.end.reached_origin is True


def test_an_unmatched_row_has_one_parent_and_no_branch(tmp_path):
    """The recorded non-match, which is what distinguishes 'no matching row
    existed' from 'matched a row whose columns are null'. Borealis' agency is
    null either way; only the absent parent tells them apart."""
    trace = trace_row(_join_run(tmp_path), "j", 1)
    assert [s.stage_id for s in trace.steps] == ["j", "filings"]
    assert trace.steps[0].branches == []
    assert trace.steps[0].row["agency"] is None
    assert trace.end.reached_origin is True


def test_spine_follows_the_right_side_when_only_it_matched(tmp_path):
    """An unmatched RIGHT row (right/outer join) has the right side as its only
    parent, so the spine follows the data rather than a left-side default."""
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
    """An aggregate's contributors are a cohort to open, not a step to take, so
    the walk ends at the stage with them reported as branches."""
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
        {"stage_id": "contracts", "row_ordinal": 0, "kind": "derivation"}
    ]


def test_branches_reach_the_render_payload(tmp_path):
    """The view offers a branch as a promotable trace; the template turns it
    into a link back into this same page at that row."""
    from app.runtime.trace import trace_to_dict
    from app.runtime.trace_view import build_trace_view
    view = build_trace_view(trace_to_dict(trace_row(_join_run(tmp_path), "j", 0)), {})
    by_stage = {n["stage_id"]: n for n in view["nodes"]}
    assert by_stage["j"]["branches"] == [
        {"stage_id": "contracts", "row_ordinal": 0, "kind": "derivation"}
    ]
    assert by_stage["filings"]["branches"] == []


def test_handler_lineage_reaches_the_executor_channel():
    """The seam: what the merge handler attaches is what the executor persists. Guards
    the `.attrs` channel against a later rebuild of the frame silently dropping
    it (the projection and the temp-column drop both rebuild)."""
    stage = Stage.model_validate({
        "id": "j", "type": "enrich", "name": "j",
        "inputs": [
            {"id": "filings", "schema": {"columns": [
                {"name": "client", "type": "str"}, {"name": "amount", "type": "int"}]}},
            {"id": "contracts", "schema": {"columns": [
                {"name": "client", "type": "str"}, {"name": "agency", "type": "str"}]}},
        ],
        "output_schema": {"columns": [{"name": "client", "type": "str"}]},
        "join": {"keys": [{"left": "client", "right": "client"}],
                  "select": ["client"]},
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


# ── crossing without a sidecar (runs recorded before lineage was captured) ────
# The positional-cross fact (app.models.stage.find_positional_cross) is owed to
# PR #348, which observed that an enrich is crossable on ordinal alone — m:1 is
# verified and an unmatched subject row survives, so output row i IS subject row
# i. That needs nothing recorded, so it reaches back to runs already on disk.


def _enrich_run_without_sidecar(tmp_path):
    return write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "contracts", "type": "input_data", "parents": [], "df": CONTRACTS},
        {"id": "j", "type": "enrich", "parents": ["filings", "contracts"], "df": JOINED},
    ])


def test_enrich_crosses_on_ordinal_with_no_sidecar(tmp_path):
    trace = trace_row(_enrich_run_without_sidecar(tmp_path), "j", 0)
    assert [s.stage_id for s in trace.steps] == ["j", "filings"]
    assert trace.end.reached_origin is True


def test_without_a_sidecar_there_is_no_branch_to_offer(tmp_path):
    """Honest rather than empty-handed: the walk still reaches the origin, it
    just cannot offer the reference side, because nothing recorded it."""
    trace = trace_row(_enrich_run_without_sidecar(tmp_path), "j", 0)
    assert trace.steps[0].branches == []


def test_columns_new_is_only_what_the_enrich_added(tmp_path):
    """Without the positional parent this read as EVERY column, overstating what
    the join contributed."""
    trace = trace_row(_enrich_run_without_sidecar(tmp_path), "j", 0)
    assert trace.steps[0].columns_new == ["agency"]


def test_expand_still_stops_without_a_sidecar(tmp_path):
    """m:n fan-out means output row i need NOT be subject row i, so there is no
    positional cross to fall back on — only a recorded one will do."""
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "contracts", "type": "input_data", "parents": [], "df": CONTRACTS},
        {"id": "j", "type": "expand", "parents": ["filings", "contracts"], "df": JOINED},
    ])
    trace = trace_row(run_dir, "j", 0)
    assert [s.stage_id for s in trace.steps] == ["j"]
    assert trace.end.reached_origin is False


def test_a_recorded_sidecar_wins_over_the_positional_fallback(tmp_path):
    """Both routes agree on the spine; only the recorded one carries the branch,
    so the fallback must not shadow it."""
    trace = trace_row(_join_run(tmp_path), "j", 0)
    assert [s.stage_id for s in trace.steps] == ["j", "filings"]
    assert trace.steps[0].branches == [RowParent("contracts", 0, EdgeKind.derivation.value)]


def test_wrong_recorded_arity_refuses_to_cross(tmp_path):
    """A missing input edge (one without a declared schema is absent from the
    manifest) makes the subject index untrustworthy — refuse, don't guess."""
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "j", "type": "enrich", "parents": ["filings"], "df": JOINED},
    ])
    trace = trace_row(run_dir, "j", 0)
    assert [s.stage_id for s in trace.steps] == ["j"]
    assert trace.end.reached_origin is False
