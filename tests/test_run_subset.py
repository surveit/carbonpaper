"""Tests for `run_subset` (app/runtime/runner.py) deriving its own executable
frontier — issue #102. The runner used to accept a caller-computed `stage_ids`
list on faith; it now takes only `target` (what to produce) and
`injected_outputs` (what's already computed), and walks the workflow graph
itself to figure out what must run, validating along the way.

Workflow under test: `a (input_data) -> b (python_row_function, doubles val)
-> c (python_row_function, +100)`, plus a sibling `d (python_row_function,
negates val)` off `a`, for the multi-target case.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.errors import SubsetRunError
from app.models import Stage, Workflow
from app.runtime.runner import run_subset


def _row_fn(id_, input_id, code):
    return Stage.model_validate({
        "id": id_, "name": id_, "type": "python_row_function",
        "inputs": [{"id": input_id}],
        "function": {"kind": "inline", "code": code},
        "output_schema": {"columns": [{"name": "id", "type": "str"},
                                      {"name": "val", "type": "int"}]},
    })


def _workflow(data_path):
    a = Stage.model_validate({
        "id": "a", "name": "a", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(data_path), "format": "csv"}},
        "output_schema": {"columns": [{"name": "id", "type": "str"},
                                      {"name": "val", "type": "int"}]},
    })
    b = _row_fn("b", "a", "def transform(row):\n"
                          "    return {'id': row['id'], 'val': row['val'] * 2}")
    c = _row_fn("c", "b", "def transform(row):\n"
                          "    return {'id': row['id'], 'val': row['val'] + 100}")
    d = _row_fn("d", "a", "def transform(row):\n"
                          "    return {'id': row['id'], 'val': -row['val']}")
    return Workflow(stages=[a, b, c, d])


@pytest.fixture
def rows_csv(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "rows.csv"
    pd.DataFrame({"id": ["x", "y"], "val": [1, 2]}).to_csv(path, index=False)
    return "data/rows.csv"


def test_run_subset_derives_the_full_ancestor_chain_with_no_injection(tmp_path, rows_csv):
    """No injected stages: the frontier for target `c` is every ancestor back
    to the source (`a`, `b`, `c`) — derived, not supplied."""
    workflow = _workflow(rows_csv)
    outputs = run_subset(workflow, target="c", injected_outputs={},
                         run_dir=tmp_path / "run", repo_root=tmp_path)

    assert set(outputs) == {"a", "b", "c"}
    assert list(outputs["c"]["val"]) == [102, 104]  # (1*2)+100, (2*2)+100


def test_run_subset_stops_the_walk_at_an_injected_stage(tmp_path, rows_csv):
    """`b` is injected: its own upstream (`a`) must NOT execute. Point `a`'s
    connector at a file that doesn't exist — if the walk crossed the injected
    node, `a` would error trying to read it; instead the run succeeds, proving
    `a` was never touched."""
    workflow = _workflow("data/does-not-exist.csv")
    injected_b = pd.DataFrame({"id": ["p", "q"], "val": [10, 20]})

    outputs = run_subset(workflow, target="c", injected_outputs={"b": injected_b},
                         run_dir=tmp_path / "run", repo_root=tmp_path)

    assert "a" not in outputs  # upstream of the injected stage never ran
    assert set(outputs) == {"b", "c"}
    assert list(outputs["c"]["val"]) == [110, 120]  # injected val + 100


def test_run_subset_supports_multiple_targets(tmp_path, rows_csv):
    """Two sibling targets off the same root: the frontier is the union of
    both ancestor chains, `a` included once."""
    workflow = _workflow(rows_csv)
    outputs = run_subset(workflow, target=["b", "d"], injected_outputs={},
                         run_dir=tmp_path / "run", repo_root=tmp_path)

    assert set(outputs) == {"a", "b", "d"}
    assert list(outputs["d"]["val"]) == [-1, -2]


def test_run_subset_rejects_an_unknown_target(tmp_path, rows_csv):
    workflow = _workflow(rows_csv)
    with pytest.raises(SubsetRunError):
        run_subset(workflow, target="ghost", injected_outputs={},
                  run_dir=tmp_path / "run", repo_root=tmp_path)


def test_run_subset_rejects_an_unknown_injected_stage(tmp_path, rows_csv):
    workflow = _workflow(rows_csv)
    with pytest.raises(SubsetRunError):
        run_subset(workflow, target="c", injected_outputs={"ghost": pd.DataFrame()},
                  run_dir=tmp_path / "run", repo_root=tmp_path)


def test_run_subset_rejects_a_target_that_is_also_injected(tmp_path, rows_csv):
    workflow = _workflow(rows_csv)
    with pytest.raises(SubsetRunError):
        run_subset(workflow, target="c", injected_outputs={"c": pd.DataFrame()},
                  run_dir=tmp_path / "run", repo_root=tmp_path)
