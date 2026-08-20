"""union/filter_rows: proves app.runtime.trace walks back to the correct source row."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import SubsetRunError
from app.core.frames import table_to_frame
from app.models import parse_stage, Stage, Workflow
from app.models.run_parameters import RunParameters
from app.runtime.executor import execute_subset
from app.runtime.trace import trace_row

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "b", "type": "int", "nullable": True}]}


def _union_stage(sid: str, input_ids: list[str]) -> Stage:
    return parse_stage({
        "id": sid, "description": sid, "type": "union",
        "inputs": [{"id": i} for i in input_ids],
        "signature": {"form": "extends", "reads": [], "adds": [], "rewrites": []},
        "union": {},
    })


def _filter_stage(sid: str, input_id: str, predicate_code: str) -> Stage:
    return parse_stage({
        "id": sid, "description": sid, "type": "filter_rows",
        "inputs": [{"id": input_id}],
        "signature": {"form": "extends",
                      "reads": [{"input": input_id, "columns": _AB_SCHEMA["columns"]}]},
        "filter": {"code": predicate_code},
    })


def _load_stage(sid: str, df: pd.DataFrame, tmp_path) -> Stage:
    """Real, not injected: execute_subset persists an output only for a stage it actually executes."""
    path = tmp_path / f"{sid}.csv"
    df.to_csv(path, index=False)
    return parse_stage({
        "id": sid, "description": sid, "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _AB_SCHEMA["columns"]},
    })


# ── union: behavior ───────────────────────────────────────────────────────────


def test_union_concatenates_two_inputs_in_declared_order(tmp_path):
    left = pd.DataFrame({"a": ["l0", "l1"], "b": [1, 2]})
    right = pd.DataFrame({"a": ["r0", "r1", "r2"], "b": [3, 4, 5]})
    load_left = _load_stage("left", left, tmp_path)
    load_right = _load_stage("right", right, tmp_path)
    union = _union_stage("u", ["left", "right"])
    workflow = Workflow(stages=[load_left, load_right, union])

    outputs = execute_subset(
        workflow, injected_outputs={},
        stage_ids=["left", "right", "u"], run_dir=tmp_path / "runs" / "r1", project_id=(tmp_path / "runs" / "r1").parent.parent.name)

    out = table_to_frame(outputs["u"])[["a", "b"]].reset_index(drop=True)
    expected = pd.concat([left, right], ignore_index=True)
    pd.testing.assert_frame_equal(out, expected)


# ── filter_rows: behavior ─────────────────────────────────────────────────────


def test_filter_rows_keeps_true_rows_in_order_with_columns_unchanged(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [1, -1, 2]})
    load = _load_stage("src", src, tmp_path)
    filt = _filter_stage("f", "src", "def should_include(row): return row['b'] > 0")
    workflow = Workflow(stages=[load, filt])

    outputs = execute_subset(
        workflow, injected_outputs={},
        stage_ids=["src", "f"], run_dir=tmp_path / "runs" / "r2", project_id=(tmp_path / "runs" / "r2").parent.parent.name)

    out = table_to_frame(outputs["f"])[["a", "b"]].reset_index(drop=True)
    expected = pd.DataFrame({"a": ["x", "z"], "b": [1, 2]})
    pd.testing.assert_frame_equal(out, expected)


def test_a_filter_that_keeps_nothing_still_feeds_its_downstream_a_valid_frame(tmp_path):
    src = pd.DataFrame({"a": ["x", "y"], "b": [1, 2]})
    load = _load_stage("src", src, tmp_path)
    filt = _filter_stage("f", "src", "def should_include(row): return row['b'] > 99")
    tag = parse_stage({
        "id": "tag", "description": "tag", "type": "python_row_function",
        "inputs": [{"id": "f"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "f", "columns": _AB_SCHEMA["columns"]}],
                      "adds": [{"name": "note", "type": "str", "nullable": False}]},
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return {**row, 'note': 'seen'}\n"},
    })
    workflow = Workflow(stages=[load, filt, tag])

    outputs = execute_subset(
        workflow, injected_outputs={},
        stage_ids=["src", "f", "tag"], run_dir=tmp_path / "runs" / "r_empty",
        project_id=(tmp_path / "runs" / "r_empty").parent.parent.name)

    out = table_to_frame(outputs["tag"])
    assert len(out) == 0
    assert list(out.columns) == ["a", "b", "note"]


def test_filter_rows_non_bool_return_is_a_loud_error(tmp_path):
    src = pd.DataFrame({"a": ["x"], "b": [1]})
    load = _load_stage("src", src, tmp_path)
    filt = _filter_stage("f", "src", "def should_include(row): return row['b']")  # int, not bool
    workflow = Workflow(stages=[load, filt])

    with pytest.raises(SubsetRunError) as exc_info:
        execute_subset(
            workflow, injected_outputs={},
            stage_ids=["src", "f"], run_dir=tmp_path / "runs" / "r3", project_id=(tmp_path / "runs" / "r3").parent.parent.name)
    assert "should_include" in str(exc_info.value)
    assert "bool" in str(exc_info.value)


# ── trace: the point of this file ─────────────────────────────────────────────


def test_trace_walks_through_filter_rows_to_the_right_source_row(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [1, -1, 2]})
    load = _load_stage("src", src, tmp_path)
    filt = _filter_stage("f", "src", "def should_include(row): return row['b'] > 0")
    workflow = Workflow(stages=[load, filt])
    run_dir = tmp_path / "runs" / "trace_filter"

    execute_subset(
        workflow, injected_outputs={},
        stage_ids=["src", "f"], run_dir=run_dir, project_id=(run_dir).parent.parent.name)

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

    execute_subset(
        workflow, injected_outputs={},
        stage_ids=["left", "right", "u"], run_dir=run_dir, project_id=(run_dir).parent.parent.name)

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


# ── the runtime's row slicing, applied to lineage as well as rows ─────────────


def test_trace_follows_lineage_after_a_limit_caps_what_the_filter_reads(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [-1, 1, 2]})
    load = _load_stage("src", src, tmp_path)
    filt = _filter_stage("f", "src", "def should_include(row): return row['b'] > 0")
    workflow = Workflow(stages=[load, filt])
    run_dir = tmp_path / "runs" / "trace_filter_limit"

    outputs = execute_subset(
        workflow, injected_outputs={},
        stage_ids=["src", "f"], run_dir=run_dir, params=RunParameters(limits={"f": 2}), project_id=(run_dir).parent.parent.name)

    # src row 2 ('z') would also have passed the predicate — it is outside the
    # window, so it was never offered to it.
    assert table_to_frame(outputs["f"])["a"].tolist() == ["y"]
    trace = trace_row(run_dir, "f", 0)
    assert trace.steps[1].row_ordinal == 1
    assert trace.steps[1].row["a"] == "y"


def test_a_row_mapper_that_may_not_drop_still_rejects_a_none_row(tmp_path):
    src = pd.DataFrame({"a": ["x"], "b": [1]})
    load = _load_stage("src", src, tmp_path)
    mapper = parse_stage({
        "id": "m", "description": "m", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "src", "columns": _AB_SCHEMA["columns"]}],
        },
        "function": {"kind": "inline", "code": "def transform(row): return None"},
    })
    workflow = Workflow(stages=[load, mapper])

    with pytest.raises(SubsetRunError) as exc_info:
        execute_subset(
            workflow, injected_outputs={},
            stage_ids=["src", "m"], run_dir=tmp_path / "runs" / "none_row",
            project_id=(tmp_path / "runs" / "none_row").parent.parent.name)
    assert "must return a dict per row" in str(exc_info.value)
