"""End-to-end tests for the positional walk: clean chains, every stop reason,
and the defensive guards."""
from __future__ import annotations

import pandas as pd
import pytest

from app.runtime.trace import trace_row
from tests.test_trace_helpers import write_run


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
    assert [h.stage_id for h in trace.hops] == ["enrich", "seeds"]
    assert [h.row_ordinal for h in trace.hops] == [1, 1]         # same ordinal
    assert trace.hops[0].row["name"] == "B"
    assert trace.hops[0].columns_new == ["score"]               # new at enrich
    assert trace.hops[0].origin == "computed"
    assert trace.hops[1].columns_new == ["facility_id", "name"]  # origin: all new
    assert trace.terminal.kind == "origin"


def test_stop_at_llm_transform_points_at_issue_61(tmp_path):
    run_dir = _chain(tmp_path, "llm_transform")
    trace = trace_row(run_dir, "enrich", 0)
    assert [h.stage_id for h in trace.hops] == ["enrich"]        # cannot cross
    assert trace.terminal.kind == "llm_transform"
    assert "#61" in trace.terminal.message


def test_stop_at_reshaping_stage_points_at_issue_58(tmp_path):
    run_dir = _chain(tmp_path, "python_frame_function")
    trace = trace_row(run_dir, "enrich", 0)
    assert [h.stage_id for h in trace.hops] == ["enrich"]
    assert trace.terminal.kind == "reshaping"
    assert "#58" in trace.terminal.message


def test_rowcount_mismatch_on_preserving_stage_stops_defensively(tmp_path):
    # 'enrich' declares python_row_function but emits fewer rows than its parent.
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"]})
    enrich = pd.DataFrame({"facility_id": ["a", "b"], "score": [1, 2]})
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ])
    trace = trace_row(run_dir, "enrich", 0)
    assert [h.stage_id for h in trace.hops] == ["enrich"]
    assert trace.terminal.kind == "rowcount_mismatch"
    assert "#58" in trace.terminal.message


def test_row_out_of_range_raises(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    with pytest.raises(ValueError, match="out of range"):
        trace_row(run_dir, "enrich", 5)


def test_unknown_stage_raises(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    with pytest.raises(ValueError, match="not in run"):
        trace_row(run_dir, "nope", 0)


def test_missing_output_file_stops(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    (run_dir / "outputs" / "seeds.parquet").unlink()
    trace = trace_row(run_dir, "enrich", 0)
    # 'enrich' shows, but crossing into 'seeds' finds no file.
    assert [h.stage_id for h in trace.hops] == ["enrich"]
    assert trace.terminal.kind == "missing_output"
    assert trace.terminal.stage_id == "seeds"


def test_preserving_stage_with_multiple_parents_stops_as_reshaping(tmp_path):
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
    assert [h.stage_id for h in trace.hops] == ["j"]
    assert trace.terminal.kind == "reshaping"
