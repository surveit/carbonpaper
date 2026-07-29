"""Behavior + lineage tests for the union and filter_rows handlers: run them for
real through run_subset (so manifest.json + outputs/*.parquet land on disk
exactly like a production run), then prove app.runtime.trace can walk through
them to the correct source row."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import SubsetRunError
from app.models import Stage, Workflow
from app.runtime.executor import run_subset
from app.runtime.trace import trace_row

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str"}, {"name": "b", "type": "int"}]}


def _union_stage(sid: str, input_ids: list[str]) -> Stage:
    return Stage.model_validate({
        "id": sid, "name": sid, "type": "union",
        "inputs": [{"id": i, "schema": _AB_SCHEMA} for i in input_ids],
        "output_schema": _AB_SCHEMA,
        "union": {},
    })


def _filter_stage(sid: str, input_id: str, predicate_code: str) -> Stage:
    return Stage.model_validate({
        "id": sid, "name": sid, "type": "filter_rows",
        "inputs": [{"id": input_id, "schema": _AB_SCHEMA}],
        "output_schema": _AB_SCHEMA,
        "filter": {"kind": "inline", "code": predicate_code},
    })


def _load_stage(sid: str, df: pd.DataFrame, tmp_path) -> Stage:
    """A REAL input_data stage backed by a csv file, not an injected output —
    the trace tests need this stage's own output persisted in the run
    directory to walk into it, which run_subset only does for a stage it
    actually executes."""
    path = tmp_path / f"{sid}.csv"
    df.to_csv(path, index=False)
    return Stage.model_validate({
        "id": sid, "name": sid, "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "csv"}},
        "output_schema": _AB_SCHEMA,
    })


# ── union: behavior ───────────────────────────────────────────────────────────


def test_union_concatenates_two_inputs_in_declared_order(tmp_path):
    left = pd.DataFrame({"a": ["l0", "l1"], "b": [1, 2]})
    right = pd.DataFrame({"a": ["r0", "r1", "r2"], "b": [3, 4, 5]})
    load_left = _load_stage("left", left, tmp_path)
    load_right = _load_stage("right", right, tmp_path)
    union = _union_stage("u", ["left", "right"])
    workflow = Workflow(stages=[load_left, load_right, union])

    outputs = run_subset(
        workflow, injected_outputs={},
        stage_ids=["left", "right", "u"], run_dir=tmp_path / "runs" / "r1", repo_root=tmp_path,
    )

    out = outputs["u"][["a", "b"]].reset_index(drop=True)
    expected = pd.concat([left, right], ignore_index=True)
    pd.testing.assert_frame_equal(out, expected)


# ── filter_rows: behavior ─────────────────────────────────────────────────────


def test_filter_rows_keeps_true_rows_in_order_with_columns_unchanged(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [1, -1, 2]})
    load = _load_stage("src", src, tmp_path)
    filt = _filter_stage("f", "src", "def should_include(row): return row['b'] > 0")
    workflow = Workflow(stages=[load, filt])

    outputs = run_subset(
        workflow, injected_outputs={},
        stage_ids=["src", "f"], run_dir=tmp_path / "runs" / "r2", repo_root=tmp_path,
    )

    out = outputs["f"][["a", "b"]].reset_index(drop=True)
    expected = pd.DataFrame({"a": ["x", "z"], "b": [1, 2]})
    pd.testing.assert_frame_equal(out, expected)


def test_filter_rows_non_bool_return_is_a_loud_error(tmp_path):
    src = pd.DataFrame({"a": ["x"], "b": [1]})
    load = _load_stage("src", src, tmp_path)
    filt = _filter_stage("f", "src", "def should_include(row): return row['b']")  # int, not bool
    workflow = Workflow(stages=[load, filt])

    with pytest.raises(SubsetRunError) as exc_info:
        run_subset(
            workflow, injected_outputs={},
            stage_ids=["src", "f"], run_dir=tmp_path / "runs" / "r3", repo_root=tmp_path,
        )
    assert "should_include" in str(exc_info.value)
    assert "bool" in str(exc_info.value)


# ── trace: the point of this file ─────────────────────────────────────────────


def test_trace_walks_through_filter_rows_to_the_right_source_row(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [1, -1, 2]})
    load = _load_stage("src", src, tmp_path)
    filt = _filter_stage("f", "src", "def should_include(row): return row['b'] > 0")
    workflow = Workflow(stages=[load, filt])
    run_dir = tmp_path / "runs" / "trace_filter"

    run_subset(
        workflow, injected_outputs={},
        stage_ids=["src", "f"], run_dir=run_dir, repo_root=tmp_path,
    )

    # f's output row 1 ('z', b=2) is src's row 2 — the dropped row ('y') sits
    # between them, so the walk must follow recorded lineage, not ordinal.
    trace = trace_row(run_dir, "f", 1)
    assert [s.stage_id for s in trace.steps] == ["f", "src"]
    assert trace.steps[0].row["a"] == "z"
    assert trace.steps[1].row_ordinal == 2
    assert trace.steps[1].row["a"] == "z"
    assert trace.end.reached_origin is True
    assert trace.end.at_stage == "src"


def test_trace_walks_through_union_to_the_right_source_row_in_the_right_input(tmp_path):
    left = pd.DataFrame({"a": ["l0", "l1"], "b": [1, 2]})
    right = pd.DataFrame({"a": ["r0", "r1", "r2"], "b": [3, 4, 5]})
    load_left = _load_stage("left", left, tmp_path)
    load_right = _load_stage("right", right, tmp_path)
    union = _union_stage("u", ["left", "right"])
    workflow = Workflow(stages=[load_left, load_right, union])
    run_dir = tmp_path / "runs" / "trace_union"

    run_subset(
        workflow, injected_outputs={},
        stage_ids=["left", "right", "u"], run_dir=run_dir, repo_root=tmp_path,
    )

    # union output row 3 is right's row 1 ('r1') — left has only 2 rows, so a
    # positional walk (matching ordinal 3 in 'left') would be plain wrong.
    trace = trace_row(run_dir, "u", 3)
    assert [s.stage_id for s in trace.steps] == ["u", "right"]
    assert trace.steps[0].row["a"] == "r1"
    assert trace.steps[1].row_ordinal == 1
    assert trace.steps[1].row["a"] == "r1"
    assert trace.end.reached_origin is True
    assert trace.end.at_stage == "right"

    # And union output row 0 traces back into 'left', not 'right'.
    trace0 = trace_row(run_dir, "u", 0)
    assert [s.stage_id for s in trace0.steps] == ["u", "left"]
    assert trace0.steps[1].row["a"] == "l0"
