"""A real run writes which branch each surviving row took, beside its lineage."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.frames import read_frame_table
from app.models import Stage, Workflow, parse_stage
from app.runtime.branches import BRANCH_SCHEMA, RowBranches
from app.runtime.row_sidecar import read_row_sidecar, resolve_row_sidecar_path
from app.runtime.executor import execute_subset

_COLS = [{"name": "a", "type": "str", "nullable": True},
         {"name": "b", "type": "int", "nullable": True}]

_TIER = """
def transform(row):
    if row["b"] > 1:
        tier = "high"
    elif row["b"] > 0:
        tier = "low"
    else:
        tier = "none"
    return dict(row, tier=tier)
"""


def _load_stage(sid: str, frame: pd.DataFrame, tmp_path) -> Stage:
    path = tmp_path / f"{sid}.csv"
    frame.to_csv(path, index=False)
    return parse_stage({
        "id": sid, "description": sid, "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _COLS},
    })


def _row_function(sid: str, input_id: str, code: str = _TIER) -> Stage:
    return parse_stage({
        "id": sid, "description": sid, "type": "python_row_function",
        "inputs": [{"id": input_id}],
        "signature": {"form": "extends",
                      "reads": [{"input": input_id, "columns": _COLS}]},
        "function": {"kind": "inline", "code": code},
    })


_MARKER = """
def transform(row):
    return dict(row, marked=1 if row["b"] > 1 else 0)
"""


def _filter(sid: str, input_id: str, code: str) -> Stage:
    return parse_stage({
        "id": sid, "description": sid, "type": "filter_rows",
        "inputs": [{"id": input_id}],
        "signature": {"form": "extends",
                      "reads": [{"input": input_id, "columns": _COLS}]},
        "filter": {"code": code},
    })


def _run(workflow: Workflow, stage_ids: list[str], run_dir):
    execute_subset(workflow, injected_outputs={}, stage_ids=stage_ids,
                   run_dir=run_dir, project_id=run_dir.parent.parent.name)


def _sidecar(run_dir, stage_id: str) -> RowBranches:
    branches = read_row_sidecar(run_dir, stage_id).branches
    assert branches is not None
    return branches


def test_a_row_function_records_the_branch_every_row_took(tmp_path) -> None:
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [5, 1, 0]})
    run_dir = tmp_path / "runs" / "r1"
    _run(Workflow(stages=[_load_stage("src", src, tmp_path), _row_function("tier", "src")]),
         ["src", "tier"], run_dir)

    assert _sidecar(run_dir, "tier").taken == [
        ("transform/0:if",), ("transform/0:elif0",), ("transform/0:else",),
    ]


def test_the_sidecar_is_indexed_by_OUTPUT_row_so_a_filter_drops_its_branches_too(tmp_path) -> None:
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [5, 1, 0]})
    keep = """
def should_include(row):
    if row["b"] > 1:
        return True
    return False
"""
    run_dir = tmp_path / "runs" / "r2"
    _run(Workflow(stages=[_load_stage("src", src, tmp_path), _filter("kept", "src", keep)]),
         ["src", "kept"], run_dir)

    # One output row, one entry — the dropped rows' branches are not carried.
    assert _sidecar(run_dir, "kept").taken == [("should_include/0:if",)]


def test_a_stage_whose_code_never_branches_records_no_branches(tmp_path) -> None:
    src = pd.DataFrame({"a": ["x", "y"], "b": [1, 2]})
    run_dir = tmp_path / "runs" / "r3"
    _run(Workflow(stages=[_load_stage("src", src, tmp_path),
                          _filter("kept", "src", "def should_include(row): return row['b'] > 0")]),
         ["src", "kept"], run_dir)

    # The stage still writes its lineage, so the file is there without the half.
    assert read_row_sidecar(run_dir, "kept").branches is None


def test_a_stage_that_only_chooses_between_values_writes_one_too(tmp_path) -> None:
    src = pd.DataFrame({"a": ["x", "y"], "b": [5, 1]})
    run_dir = tmp_path / "runs" / "r3b"
    _run(Workflow(stages=[_load_stage("src", src, tmp_path),
                          _row_function("marked", "src", _MARKER)]),
         ["src", "marked"], run_dir)

    assert _sidecar(run_dir, "marked").taken == [
        ("transform/0:choice0:if",), ("transform/0:choice0:else",),
    ]


def test_a_stage_that_records_only_branches_writes_only_that_column(tmp_path) -> None:
    src = pd.DataFrame({"a": ["x"], "b": [5]})
    run_dir = tmp_path / "runs" / "r4"
    _run(Workflow(stages=[_load_stage("src", src, tmp_path), _row_function("tier", "src")]),
         ["src", "tier"], run_dir)

    table = read_frame_table(resolve_row_sidecar_path(run_dir, "tier"))
    assert table.schema.equals(BRANCH_SCHEMA)


_BOOM = """
def transform(row):
    if row["b"] > 1:
        tier = "high"
    else:
        tier = "low"
    if row["a"] == "y":
        raise ValueError("row y is refused")
    return dict(row, tier=tier)
"""


def test_a_row_that_raises_does_not_lend_its_branches_to_the_next_row() -> None:
    from app.runtime.branches import BranchRecorder
    from app.runtime.code import load_function
    from app.runtime.stages.execution import RecordingRowMapper, _RowsInGroupsOfOne

    recorder = BranchRecorder()
    transform = load_function(_BOOM, "transform", "transform", recorder)
    assert transform is not None
    group = _RowsInGroupsOfOne(RecordingRowMapper(lambda row, index: transform(row), recorder))
    for index, row in enumerate([{"a": "y", "b": 5}, {"a": "z", "b": 0}]):
        try:
            group([index], [dict(row)])
        except ValueError:
            pass
    assert recorder.branches_for(0) == ("transform/0:if", "transform/1:if")
    assert recorder.branches_for(1) == ("transform/0:else",)


def test_a_row_left_open_is_refused_rather_than_merged_into_the_next() -> None:
    from app.runtime.branches import BranchRecorder
    from app.runtime.errors import BranchRecordingError

    recorder = BranchRecorder()
    recorder.open_row(0)
    with pytest.raises(BranchRecordingError, match="never closed"):
        recorder.open_row(1)


def test_a_branch_reported_outside_a_row_is_refused() -> None:
    from app.runtime.branches import BranchRecorder
    from app.runtime.errors import BranchRecordingError

    with pytest.raises(BranchRecordingError, match="outside a row"):
        BranchRecorder().record("transform/0:if")


def test_closing_without_opening_is_refused() -> None:
    from app.runtime.branches import BranchRecorder
    from app.runtime.errors import BranchRecordingError

    with pytest.raises(BranchRecordingError, match="no row is open"):
        BranchRecorder().close_row()
