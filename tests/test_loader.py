"""The canonical compiled-stage loader: tolerant per-file for the viewer,
strict (reject the whole workflow) for the runner."""
from __future__ import annotations

import json

import pytest

from app.errors import WorkflowLoadError
from app.services.loader import (
    load_compiled_dir,
    load_workflow,
)

VALID = {
    "id": "load", "name": "Load", "type": "input_data",
    "connector": {"kind": "file", "params": {"path": "data/items.csv", "format": "csv"}},
}
INVALID = {  # file connector without params.path
    "id": "bad", "name": "Bad", "type": "input_data",
    "connector": {"kind": "file", "params": {"format": "csv"}},
}


def _write(root, name, data):
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "compiled" / name).write_text(json.dumps(data), encoding="utf-8")


def test_tolerant_load_reports_per_file_issues(tmp_path):
    _write(tmp_path, "01_load.json", VALID)
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
    _write(tmp_path, "01_load.json", VALID)
    [stage] = load_workflow(tmp_path)
    assert stage.id == "load"


def test_strict_load_raises_with_all_issues(tmp_path):
    _write(tmp_path, "01_load.json", VALID)
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


def test_strict_load_rejects_missing_or_empty_compiled_dir(tmp_path):
    """A typo'd project path must fail loudly, not produce a valid 0-stage workflow."""
    with pytest.raises(WorkflowLoadError, match="no compiled stage files"):
        load_workflow(tmp_path)  # no compiled/ dir at all
    (tmp_path / "compiled").mkdir()
    with pytest.raises(WorkflowLoadError, match="no compiled stage files"):
        load_workflow(tmp_path)  # compiled/ exists but is empty
