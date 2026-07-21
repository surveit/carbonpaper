"""The canonical compiled-stage loader: tolerant per-file for the viewer,
strict (reject the whole workflow) for the runner."""
from __future__ import annotations

import json

import pytest

from app.services.loader import (
    WorkflowLoadError,
    load_compiled_dir,
    load_workflow,
)

def _valid(tmp_path):
    return {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}},
    }


INVALID = {  # file connector params.path is relative, not absolute
    "id": "bad", "name": "Bad", "type": "input_data",
    "connector": {"kind": "file", "params": {"path": "data/items.csv", "format": "csv"}},
}


def _write(root, name, data):
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "compiled" / name).write_text(json.dumps(data), encoding="utf-8")


def test_tolerant_load_reports_per_file_issues(tmp_path):
    _write(tmp_path, "01_load.json", _valid(tmp_path))
    _write(tmp_path, "02_bad.json", INVALID)
    entries = load_compiled_dir(tmp_path / "compiled")
    assert [e.filename for e in entries] == ["01_load.json", "02_bad.json"]
    assert entries[0].stage is not None and not entries[0].issues
    assert entries[1].stage is None and entries[1].issues


def test_tolerant_load_handles_unparseable_json(tmp_path):
    (tmp_path / "compiled").mkdir(parents=True)
    (tmp_path / "compiled" / "01_broken.json").write_text('{"id": unclosed', encoding="utf-8")
    [entry] = load_compiled_dir(tmp_path / "compiled")
    assert entry.stage is None
    assert any("parse error" in i.lower() for i in entry.issues)


def test_strict_load_returns_stages(tmp_path):
    _write(tmp_path, "01_load.json", _valid(tmp_path))
    [stage] = load_workflow(tmp_path)
    assert stage.id == "load"


def test_strict_load_raises_with_all_issues(tmp_path):
    _write(tmp_path, "01_load.json", _valid(tmp_path))
    _write(tmp_path, "02_bad.json", INVALID)
    with pytest.raises(WorkflowLoadError) as exc:
        load_workflow(tmp_path)
    assert any("02_bad.json" in i for i in exc.value.issues)


def test_strict_load_catches_cross_stage_issues(tmp_path):
    dangling = {"id": "x", "name": "X", "type": "python_frame_function",
                "inputs": [{"id": "missing_upstream"}],
                "function": {"kind": "inline", "code": "def transform(row): return row"}}
    _write(tmp_path, "01_x.json", dangling)
    with pytest.raises(WorkflowLoadError) as exc:
        load_workflow(tmp_path)
    assert any("missing_upstream" in i for i in exc.value.issues)


def test_strict_load_folds_cascading_dangling_inputs_into_root_cause(tmp_path):
    """One invalid upstream stage whose downstream consumers all dangle collapses
    to ONE root-cause issue line, not one line per cascaded consumer (issue #162:
    4 root causes must read as 4 problems, not N reported lines)."""
    _write(tmp_path, "01_bad.json", INVALID)  # id="bad" — fails path validation
    for i in range(4):
        _write(tmp_path, f"0{i + 2}_down{i}.json", {
            "id": f"down{i}", "name": f"Down{i}", "type": "python_frame_function",
            "inputs": [{"id": "bad"}],
            "function": {"kind": "inline", "code": "def transform(row): return row"},
        })
    with pytest.raises(WorkflowLoadError) as exc:
        load_workflow(tmp_path)
    issues = exc.value.issues
    # The broken file's own error, ONE line — not 1 (file) + 4 (one per dangling
    # downstream consumer).
    assert len(issues) == 1
    assert "01_bad.json" in issues[0]
    for i in range(4):
        assert f"down{i}" in issues[0]


def test_strict_load_groups_dangling_inputs_with_no_matching_file(tmp_path):
    """Multiple stages referencing the SAME unknown id (nothing failed to load —
    the id is simply wrong) still collapse to one line, not one per consumer."""
    for i in range(3):
        _write(tmp_path, f"0{i + 1}_down{i}.json", {
            "id": f"down{i}", "name": f"Down{i}", "type": "python_frame_function",
            "inputs": [{"id": "ghost"}],
            "function": {"kind": "inline", "code": "def transform(row): return row"},
        })
    with pytest.raises(WorkflowLoadError) as exc:
        load_workflow(tmp_path)
    issues = exc.value.issues
    assert len(issues) == 1
    assert "ghost" in issues[0]
    for i in range(3):
        assert f"down{i}" in issues[0]


def test_strict_load_rejects_missing_or_empty_compiled_dir(tmp_path):
    """A typo'd project path must fail loudly, not produce a valid 0-stage workflow."""
    with pytest.raises(WorkflowLoadError, match="no compiled stage files"):
        load_workflow(tmp_path)  # no compiled/ dir at all
    (tmp_path / "compiled").mkdir()
    with pytest.raises(WorkflowLoadError, match="no compiled stage files"):
        load_workflow(tmp_path)  # compiled/ exists but is empty
