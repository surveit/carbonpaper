"""RunFrames: tracing many rows of one run reads each file once, and says the same
thing as tracing them one at a time."""
from __future__ import annotations

import pandas as pd

import app.runtime.trace as trace_module
from app.runtime.lineage import RowLineage, RowParent
from app.runtime.trace import RunFrames, trace_row, trace_row_from, trace_to_dict
from test_trace_helpers import write_run


def _filtered_run(tmp_path):
    """A filter_rows keeps rows 0 and 2, so crossing it takes the sidecar, not the ordinal."""
    seeds = pd.DataFrame({"filing_id": ["f1", "f2", "f3"], "client": ["A", "B", "C"]})
    kept = seeds.iloc[[0, 2]].reset_index(drop=True)
    return write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "kept", "type": "filter_rows", "parents": ["seeds"], "df": kept,
         "lineage": RowLineage([[RowParent("seeds", 0)], [RowParent("seeds", 2)]])},
    ])


def test_shared_reader_traces_identically_to_the_one_shot_call(tmp_path):
    run_dir = _filtered_run(tmp_path)
    frames = RunFrames(run_dir)
    for row in range(2):
        assert trace_to_dict(trace_row_from(frames, "kept", row)) == trace_to_dict(
            trace_row(run_dir, "kept", row)
        )


def test_the_filter_is_crossed_to_the_true_source_row(tmp_path):
    run_dir = _filtered_run(tmp_path)
    trace = trace_row_from(RunFrames(run_dir), "kept", 1)
    assert [s.stage_id for s in trace.steps] == ["kept", "seeds"]
    assert [s.row_ordinal for s in trace.steps] == [1, 2]  # row 1 of kept was row 2 of seeds
    assert trace.steps[1].row["filing_id"] == "f3"
    assert trace.end.reached_origin is True


def test_each_output_and_sidecar_is_read_once_however_many_rows_are_traced(
    tmp_path, monkeypatch
):
    run_dir = _filtered_run(tmp_path)
    reads: list[str] = []
    real = trace_module.read_frame_table
    monkeypatch.setattr(
        trace_module, "read_frame_table",
        lambda path: (reads.append(path.name), real(path))[1],
    )
    frames = RunFrames(run_dir)
    for row in range(2):
        trace_row_from(frames, "kept", row)
    # Two outputs and one sidecar. Re-reading is what made a trace cost seconds.
    assert sorted(reads) == ["kept.lineage.parquet", "kept.parquet", "seeds.parquet"]
