"""Tests for app/services/project.py's list_project_names."""
import json
from pathlib import Path

from app.services import project as project_service


def _write_minimal_stage(compiled: Path, sid: str) -> None:
    compiled.mkdir(parents=True, exist_ok=True)
    stage = {
        "id": sid,
        "name": f"{sid} step",
        "type": "input_data",
        "connector": {"kind": "file", "params": {"path": "data/items.csv", "format": "csv"}},
    }
    (compiled / f"01_{sid}.json").write_text(json.dumps(stage), encoding="utf-8")


def test_list_project_names_only_dirs_with_compiled(tmp_path: Path) -> None:
    _write_minimal_stage(tmp_path / "alpha" / "compiled", "load")
    (tmp_path / "not_a_project").mkdir()
    assert project_service.list_project_names(tmp_path) == ["alpha"]


def test_list_project_names_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert project_service.list_project_names(tmp_path / "does_not_exist") == []
