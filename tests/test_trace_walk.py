"""End-to-end tests for the positional walk: clean chains, the stop cases, and
the defensive guards."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import RowOutOfRange, StageNotInRun
from app.runtime.trace import trace_row
from test_trace_helpers import write_run


def _chain(tmp_path, second_type: str):
    """A two-stage run: input_data 'seeds' -> `second_type` 'enrich', 3 rows,
    positional. 'enrich' adds a 'score' column."""
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"], "name": ["A", "B", "C"]})
    enrich = seeds.assign(score=[10, 20, 30])
    return write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": second_type, "parents": ["seeds"], "df": enrich},
    ])


def test_row_preserving_chain_traces_to_origin(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    trace = trace_row(run_dir, "enrich", 1)
    assert [s.stage_id for s in trace.steps] == ["enrich", "seeds"]
    assert [s.row_ordinal for s in trace.steps] == [1, 1]         # same ordinal
    assert trace.steps[0].row["name"] == "B"
    assert trace.steps[0].columns_new == ["score"]               # new at enrich
    assert trace.steps[0].origin == "computed"
    assert trace.steps[1].columns_new == ["facility_id", "name"]  # origin: all new
    assert trace.end.reached_origin is True


def test_llm_transform_traces_positionally(tmp_path):
    # llm_transform is strictly 1:1 and order-preserving (PR #29), so the walk
    # crosses it on ordinal alone, like python_row_function (closes #61).
    run_dir = _chain(tmp_path, "llm_transform")
    trace = trace_row(run_dir, "enrich", 1)
    assert [s.stage_id for s in trace.steps] == ["enrich", "seeds"]
    assert [s.row_ordinal for s in trace.steps] == [1, 1]         # same ordinal
    assert trace.steps[0].row["name"] == "B"
    assert trace.steps[0].columns_new == ["score"]               # new at enrich
    assert trace.steps[0].origin == "llm"
    assert trace.end.reached_origin is True


def test_human_review_queue_traces_positionally(tmp_path):
    # The queue handler is driven per row and emits every input row — a rejected
    # row stays, carrying its rejection — so output row i is input row i and the
    # walk crosses it on ordinal alone.
    run_dir = _chain(tmp_path, "human_review_queue")
    trace = trace_row(run_dir, "enrich", 1)
    assert [s.stage_id for s in trace.steps] == ["enrich", "seeds"]
    assert [s.row_ordinal for s in trace.steps] == [1, 1]         # same ordinal
    assert trace.steps[0].row["name"] == "B"
    assert trace.end.reached_origin is True


def _join_run(tmp_path, join_type: str):
    """A join run: subject 'permits' (3 rows) + reference 'owners', joined by
    `join_type` into 'joined', which carries one row per subject row in subject
    order and gains the reference's 'owner' column."""
    permits = pd.DataFrame({"facility_id": ["a", "b", "c"], "name": ["A", "B", "C"]})
    owners = pd.DataFrame({"facility_id": ["a", "b", "c"], "owner": ["X", "Y", "Z"]})
    joined = permits.assign(owner=["X", "Y", "Z"])
    return write_run(tmp_path, [
        {"id": "permits", "type": "input_data", "parents": [], "df": permits},
        {"id": "owners", "type": "input_data", "parents": [], "df": owners},
        {"id": "joined", "type": join_type,
         "parents": ["permits", "owners"], "df": joined},
    ])


def test_enrich_crosses_into_its_subject_input(tmp_path):
    # enrich is m:1 and never drops a subject row, so output row i IS subject
    # row i — the walk crosses it on ordinal into inputs[0], ignoring the
    # reference. It is NOT row-DRIVEN (a join gets whole frames), which is why
    # this rides find_positional_cross and not is_grain_and_order_preserving.
    run_dir = _join_run(tmp_path, "enrich")
    trace = trace_row(run_dir, "joined", 1)
    assert [s.stage_id for s in trace.steps] == ["joined", "permits"]
    assert [s.row_ordinal for s in trace.steps] == [1, 1]
    assert trace.steps[0].row["name"] == "B"
    assert trace.steps[1].row["name"] == "B"        # landed on the right subject row
    assert trace.end.reached_origin is True


def test_enrich_reports_only_the_joined_in_columns_as_new(tmp_path):
    # columns_new is taken against the SUBJECT, so the column the join brought
    # over reads as new at the join and the subject's own columns do not. Before
    # the subject was resolvable this reported every column as new.
    run_dir = _join_run(tmp_path, "enrich")
    trace = trace_row(run_dir, "joined", 0)
    assert trace.steps[0].columns_new == ["owner"]


def test_expand_still_stops_because_it_fans_out(tmp_path):
    # m:n: output row i need not be subject row i, so there is nothing to cross
    # on until expand records per-row lineage of its own.
    run_dir = _join_run(tmp_path, "expand")
    trace = trace_row(run_dir, "joined", 0)
    assert [s.stage_id for s in trace.steps] == ["joined"]
    assert trace.end.reached_origin is False
    assert "#58" in trace.end.message


def test_enrich_with_one_recorded_edge_refuses_to_guess(tmp_path):
    # A manifest disagreeing with the type (one edge where enrich has two) must
    # not be indexed into: the single edge might be the REFERENCE, and crossing
    # into it would report a confidently wrong ancestor.
    permits = pd.DataFrame({"facility_id": ["a", "b"], "name": ["A", "B"]})
    joined = permits.assign(owner=["X", "Y"])
    run_dir = write_run(tmp_path, [
        {"id": "owners", "type": "input_data", "parents": [], "df": permits},
        {"id": "joined", "type": "enrich", "parents": ["owners"], "df": joined},
    ])
    trace = trace_row(run_dir, "joined", 0)
    assert [s.stage_id for s in trace.steps] == ["joined"]
    assert trace.end.reached_origin is False


def test_stop_at_reshaping_stage_points_at_issue_58(tmp_path):
    run_dir = _chain(tmp_path, "python_frame_function")
    trace = trace_row(run_dir, "enrich", 0)
    assert [s.stage_id for s in trace.steps] == ["enrich"]
    assert trace.end.reached_origin is False
    assert "#58" in trace.end.message


def test_rowcount_mismatch_on_preserving_stage_stops_defensively(tmp_path):
    # The point-5 scenario: a row-preserving stage whose PERSISTED output has
    # fewer rows than its input (a row errored out, or --limit sliced it), so
    # output row i no longer positionally equals input row i. The walk must
    # refuse to guess — stop at this step, don't map to the wrong parent row.
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"]})          # N = 3
    enrich = pd.DataFrame({"facility_id": ["a", "b"], "score": [1, 2]})  # M = 2 < N
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ])
    trace = trace_row(run_dir, "enrich", 0)
    assert [s.stage_id for s in trace.steps] == ["enrich"]          # enrich shown
    assert trace.end.reached_origin is False                       # but not crossed
    assert "#58" in trace.end.message


def test_mismatch_deeper_in_chain_stops_at_the_right_step(tmp_path):
    # A(3) -> B(2, dropped) -> C(2): C<-B is fine (2==2) but B<-A breaks (2!=3),
    # proving the guard fires exactly at the step where counts diverge.
    a = pd.DataFrame({"k": ["a", "b", "c"]})                        # 3
    b = pd.DataFrame({"k": ["a", "b"], "x": [1, 2]})               # 2  (dropped one)
    c = pd.DataFrame({"k": ["a", "b"], "x": [1, 2], "y": [9, 8]})  # 2
    run_dir = write_run(tmp_path, [
        {"id": "a", "type": "input_data", "parents": [], "df": a},
        {"id": "b", "type": "python_row_function", "parents": ["a"], "df": b},
        {"id": "c", "type": "python_row_function", "parents": ["b"], "df": c},
    ])
    trace = trace_row(run_dir, "c", 0)
    assert [s.stage_id for s in trace.steps] == ["c", "b"]
    assert trace.end.reached_origin is False
    assert trace.end.at_stage == "b"


def test_row_out_of_range_raises(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    with pytest.raises(RowOutOfRange, match="out of range"):
        trace_row(run_dir, "enrich", 5)


def test_unknown_stage_raises(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    with pytest.raises(StageNotInRun, match="not in run"):
        trace_row(run_dir, "nope", 0)


def test_missing_output_file_stops(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    (run_dir / "outputs" / "seeds.parquet").unlink()
    trace = trace_row(run_dir, "enrich", 0)
    # 'enrich' shows, but crossing into 'seeds' finds no file.
    assert [s.stage_id for s in trace.steps] == ["enrich"]
    assert trace.end.reached_origin is False
    assert trace.end.at_stage == "seeds"


def test_preserving_stage_with_multiple_parents_stops(tmp_path):
    left = pd.DataFrame({"k": ["a", "b"]})
    right = pd.DataFrame({"k": ["a", "b"]})
    joined = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
    run_dir = write_run(tmp_path, [
        {"id": "left", "type": "input_data", "parents": [], "df": left},
        {"id": "right", "type": "input_data", "parents": [], "df": right},
        # Mislabeled as row-preserving but has two parents: not positional.
        {"id": "j", "type": "python_row_function", "parents": ["left", "right"], "df": joined},
    ])
    trace = trace_row(run_dir, "j", 0)
    assert [s.stage_id for s in trace.steps] == ["j"]
    assert trace.end.reached_origin is False
