"""The same row, its path re-told through a parent the walk did not follow."""
from __future__ import annotations

import pytest

from app.core.errors import TraceViaNotRecorded
from app.runtime.trace import RunFrames, TraceVia, trace_row, trace_row_via
from test_trace_join_branches import _join_run
from test_trace_stories import _summed_run

_ACME, _BOREALIS = 0, 1


def _via(run_dir, stage: str, row: int, *vias: TraceVia):
    return trace_row_via(RunFrames(run_dir), stage, row, list(vias))


def test_the_row_the_page_names_is_still_where_the_path_ends(tmp_path):
    run_dir = _join_run(tmp_path)
    trace = _via(run_dir, "j", 0, TraceVia(step=2, stage_id="contracts", row_ordinal=0))

    assert (trace.start_stage, trace.start_row) == ("j", 0)
    # Newest first: the claim, then the reference row's own ancestry in place of the subject's.
    assert [s.stage_id for s in trace.steps] == ["j", "contracts"]
    assert trace.steps[1].row["agency"] == "HHS"
    assert trace.end.reached_origin is True


def test_routing_through_a_contributor_carries_a_summary_row_past_its_fan_in(tmp_path):
    run_dir = _summed_run(tmp_path)
    assert trace_row(run_dir, "totals", 0).end.reached_origin is False

    carried = _via(run_dir, "totals", 0,
                   TraceVia(step=1, stage_id="filings", row_ordinal=_BOREALIS))
    assert [s.stage_id for s in carried.steps] == ["totals", "filings"]
    assert carried.steps[1].row_ordinal == _BOREALIS
    assert carried.end.reached_origin is True


def test_a_parent_this_run_never_recorded_is_refused(tmp_path):
    run_dir = _join_run(tmp_path)
    with pytest.raises(TraceViaNotRecorded, match="not a recorded parent"):
        _via(run_dir, "j", 0, TraceVia(step=2, stage_id="contracts", row_ordinal=1))


def test_a_step_the_path_does_not_reach_is_refused(tmp_path):
    run_dir = _join_run(tmp_path)
    with pytest.raises(TraceViaNotRecorded, match="not on a path"):
        _via(run_dir, "j", 0, TraceVia(step=9, stage_id="contracts", row_ordinal=0))


def test_the_row_it_summarizes_is_never_told_as_the_row_it_was_made_from(tmp_path):
    """An aggregate has no single parent, so routing through one must not diff against it."""
    from app.web.panel_links import AppPanelLinks
    from app.runtime.trace import trace_to_dict
    from app.web.trace_view import build_trace_view

    carried = _via(_summed_run(tmp_path), "totals", 0,
                   TraceVia(step=1, stage_id="filings", row_ordinal=_ACME))
    view = build_trace_view(trace_to_dict(carried), {}, AppPanelLinks("proj", "T1"))

    claim = view["nodes"][-1]
    assert claim["stage_id"] == "totals"
    assert claim["base"] is None
    # Every parent it summarizes is still counted, including the one now on the path.
    assert [g["total"] for g in claim["contributor_groups"]] == [2]
