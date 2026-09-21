"""The stage loader: per-stage for the viewer, whole-workflow for the runner."""
from __future__ import annotations

import pytest

from app.services.loader import WorkflowLoadError, load_stage_entries, load_workflow
from stage_seed import set_stages


def _valid(tmp_path):
    return {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "items.csv"), "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [{"name": "k", "type": "str", "nullable": True}],
        },
    }


INVALID = {  # file connector params.path is relative, not absolute
    "id": "bad", "description": "Bad", "type": "input_data",
    "connector": {"kind": "file", "params": {"path": "data/items.csv", "format": "csv"}},
    "signature": {"form": "replaces", "produces": [{"name": "k", "type": "str", "nullable": True}]},
}


def _store(project, *specs):
    set_stages(project, list(specs))


def test_tolerant_load_reports_per_stage_issues(tmp_path):
    _store("p", _valid(tmp_path), INVALID)
    entries = load_stage_entries("p")
    assert [e.label for e in entries] == ["load", "bad"]
    assert entries[0].stage is not None and not entries[0].issues
    assert entries[1].stage is None and entries[1].issues


def test_a_stage_with_no_usable_id_is_labelled_by_position(tmp_path):
    _store("p", {"type": "input_data"})
    [entry] = load_stage_entries("p")
    assert entry.label == "stage #1"
    assert entry.stage is None and entry.issues


def test_strict_load_returns_stages(tmp_path):
    _store("p", _valid(tmp_path))
    [stage] = load_workflow("p")
    assert stage.id == "load"


def test_strict_load_raises_with_all_issues(tmp_path):
    _store("p", _valid(tmp_path), INVALID)
    with pytest.raises(WorkflowLoadError) as exc:
        load_workflow("p")
    assert any(i.startswith("bad:") for i in exc.value.issues)


def test_strict_load_catches_cross_stage_issues(tmp_path):
    dangling = {"id": "x", "description": "X", "type": "python_frame_function",
                "inputs": [{"id": "missing_upstream"}],
                "function": {"kind": "inline", "code": "def transform(row): return row"},
                "signature": {
                    "form": "replaces",
                    "reads": [
                        {
                            "input": "missing_upstream",
                            "columns": [{"name": "k", "type": "str", "nullable": True}],
                        },
                    ],
                    "produces": [{"name": "k", "type": "str", "nullable": True}],
                }}
    _store("p", dangling)
    with pytest.raises(WorkflowLoadError) as exc:
        load_workflow("p")
    assert any("missing_upstream" in i for i in exc.value.issues)


def test_strict_load_rejects_an_unstored_or_empty_project():
    """A typo'd project name must fail loudly, not produce a valid 0-stage workflow."""
    with pytest.raises(WorkflowLoadError, match="has no stages"):
        load_workflow("never_stored")
    _store("empty")
    with pytest.raises(WorkflowLoadError, match="has no stages"):
        load_workflow("empty")
