"""The canonical compiled-stage loader: tolerant per-file for the viewer,
strict (reject the whole DAG) for the runner."""
from __future__ import annotations

import pytest
import yaml

from app.models.loader import (
    MethodologyLoadError,
    load_compiled_dir,
    load_methodology_stages,
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
    (root / "compiled" / name).write_text(yaml.safe_dump(data), encoding="utf-8")


def test_tolerant_load_reports_per_file_issues(tmp_path):
    _write(tmp_path, "01_load.yaml", VALID)
    _write(tmp_path, "02_bad.yaml", INVALID)
    entries = load_compiled_dir(tmp_path / "compiled")
    assert [e.filename for e in entries] == ["01_load.yaml", "02_bad.yaml"]
    assert entries[0].stage is not None and not entries[0].issues
    assert entries[1].stage is None and entries[1].issues


def test_tolerant_load_handles_unparseable_yaml(tmp_path):
    (tmp_path / "compiled").mkdir(parents=True)
    (tmp_path / "compiled" / "01_broken.yaml").write_text("a: [unclosed", encoding="utf-8")
    [entry] = load_compiled_dir(tmp_path / "compiled")
    assert entry.stage is None
    assert any("parse error" in i.lower() for i in entry.issues)


def test_strict_load_returns_stages(tmp_path):
    _write(tmp_path, "01_load.yaml", VALID)
    [stage] = load_methodology_stages(tmp_path)
    assert stage.id == "load"


def test_strict_load_raises_with_all_issues(tmp_path):
    _write(tmp_path, "01_load.yaml", VALID)
    _write(tmp_path, "02_bad.yaml", INVALID)
    with pytest.raises(MethodologyLoadError) as exc:
        load_methodology_stages(tmp_path)
    assert any("02_bad.yaml" in i for i in exc.value.issues)


def test_strict_load_catches_cross_stage_issues(tmp_path):
    dangling = {"id": "x", "name": "X", "type": "llm_transform",
                "inputs": [{"id": "missing_upstream"}],
                "llm": {"prompt_template": "hi"}}
    _write(tmp_path, "01_x.yaml", dangling)
    with pytest.raises(MethodologyLoadError) as exc:
        load_methodology_stages(tmp_path)
    assert any("missing_upstream" in i for i in exc.value.issues)
