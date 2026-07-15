"""apply_input_bindings: per-run input bindings merged into just-loaded stages.

Pure-function tests here; prepare_run integration (manifest provenance, sha256,
no-run-dir-on-failure) is in this file too.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from app.core.errors import MissingInputBindingError
from app.core.models import Stage
from app.runtime.runner import apply_input_bindings, execute_run
from app.services.versioning import create_version


def _input_stage(stage_id: str, path: str | None) -> Stage:
    params: dict = {"path": path, "format": "csv"} if path else {}
    return Stage.model_validate({
        "id": stage_id, "name": stage_id, "type": "input_data",
        "connector": {"kind": "file", "params": params},
    })


def test_run_binding_overrides_workflow_path(tmp_path):
    authored, bound = str(tmp_path / "a.csv"), str(tmp_path / "b.csv")
    stages, resolved = apply_input_bindings(
        [_input_stage("load", authored)], {"load": bound})
    assert stages[0].connector.params["path"] == bound
    assert resolved["load"] == {"params": {"path": bound, "format": "csv"},
                                "source": "run"}


def test_workflow_path_used_when_no_binding(tmp_path):
    authored = str(tmp_path / "a.csv")
    _, resolved = apply_input_bindings([_input_stage("load", authored)], None)
    assert resolved["load"]["source"] == "workflow"
    assert resolved["load"]["params"]["path"] == authored


def test_dict_binding_merges_over_params(tmp_path):
    bound = str(tmp_path / "b.parquet")
    stages, _ = apply_input_bindings(
        [_input_stage("load", str(tmp_path / "a.csv"))],
        {"load": {"path": bound, "format": "parquet"}})
    assert stages[0].connector.params == {"path": bound, "format": "parquet"}


def test_relative_binding_path_rejected(tmp_path):
    with pytest.raises(ValueError, match="load"):
        apply_input_bindings([_input_stage("load", None)], {"load": "data/items.csv"})


def test_unknown_binding_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="nope"):
        apply_input_bindings(
            [_input_stage("load", str(tmp_path / "a.csv"))],
            {"nope": str(tmp_path / "b.csv")})


def test_unbound_inputs_all_named(tmp_path):
    with pytest.raises(MissingInputBindingError) as exc:
        apply_input_bindings(
            [_input_stage("load_a", None), _input_stage("load_b", None)], None)
    assert "load_a" in str(exc.value) and "load_b" in str(exc.value)


def test_original_stages_untouched(tmp_path):
    authored = str(tmp_path / "a.csv")
    original = _input_stage("load", authored)
    apply_input_bindings([original], {"load": str(tmp_path / "b.csv")})
    assert original.connector.params["path"] == authored


def _make_bound_project(root, filename="a.csv"):
    (root / "compiled").mkdir(parents=True)
    data = root / filename
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    stage = {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    create_version(root, message="seed", reviewer="test")
    return data


def test_run_binding_recorded_with_hash_and_source(tmp_path):
    _make_bound_project(tmp_path)
    other = tmp_path / "b.csv"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_csv(other, index=False)

    manifest = execute_run(tmp_path, repo_root=tmp_path,
                           bindings={"load": str(other)})

    assert manifest["status"] == "ok"
    rec = manifest["input_bindings"]["load"]
    assert rec["path"] == str(other)
    assert rec["source"] == "run"
    assert rec["sha256"] == hashlib.sha256(other.read_bytes()).hexdigest()
    assert rec["bytes"] == other.stat().st_size
    out = pd.read_parquet(tmp_path / "runs" / manifest["run_id"] / "outputs" / "load.parquet")
    assert list(out["val"]) == [9]                     # read the BOUND file


def test_workflow_path_recorded_as_workflow_source(tmp_path):
    data = _make_bound_project(tmp_path)
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    rec = manifest["input_bindings"]["load"]
    assert rec["source"] == "workflow"
    assert rec["path"] == str(data)


def test_unbound_input_leaves_no_run_dir(tmp_path):
    (tmp_path / "compiled").mkdir(parents=True)
    stage = {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file", "params": {}}}
    (tmp_path / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    create_version(tmp_path, message="seed", reviewer="test")

    with pytest.raises(MissingInputBindingError, match="load"):
        execute_run(tmp_path, repo_root=tmp_path)
    assert not (tmp_path / "runs").exists()


def test_bound_file_must_exist_before_run_dir(tmp_path):
    _make_bound_project(tmp_path)
    with pytest.raises(FileNotFoundError, match="ghost"):
        execute_run(tmp_path, repo_root=tmp_path,
                    bindings={"load": str(tmp_path / "ghost.csv")})
    assert not (tmp_path / "runs").exists()
