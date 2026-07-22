"""Run bindings: per-run connector-param overrides merged into just-loaded
stages (apply_run_bindings, generic), and the stage-owned preflight that judges
run-readiness and records provenance (validate_stages_ready + PREFLIGHTS).

Pure-function tests plus prepare_run integration (manifest provenance, sha256,
no-run-dir-on-failure).
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from app.core.errors import MissingInputBindingError
from app.models import Stage
from app.runtime.runner import apply_run_bindings, validate_stages_ready, execute_run
from app.runtime.stages.input_data import read_input_data
from app.services import versioning
from app.services.versioning import create_version_from_disk


def _input_stage(stage_id: str, path: str | None) -> Stage:
    params: dict = {"path": path, "format": "csv"} if path else {}
    return Stage.model_validate({
        "id": stage_id, "name": stage_id, "type": "input_data",
        "connector": {"kind": "file", "params": params},
    })


def _connectorless_stage(stage_id: str, input_id: str) -> Stage:
    return Stage.model_validate({
        "id": stage_id, "name": stage_id, "type": "python_row_function",
        "inputs": [input_id],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    })


# ── apply_run_bindings: generic param overrides, no file knowledge ──────────

def test_run_binding_overrides_workflow_params(tmp_path):
    authored, bound = str(tmp_path / "a.csv"), str(tmp_path / "b.csv")
    stages, sources = apply_run_bindings(
        [_input_stage("load", authored)], {"load": {"path": bound}})
    assert stages[0].connector.params["path"] == bound
    assert sources == {"load": "run"}


def test_workflow_params_used_when_no_binding(tmp_path):
    authored = str(tmp_path / "a.csv")
    stages, sources = apply_run_bindings([_input_stage("load", authored)], None)
    assert sources == {"load": "workflow"}
    assert stages[0].connector.params["path"] == authored


def test_binding_merges_over_params(tmp_path):
    bound = str(tmp_path / "b.parquet")
    stages, _ = apply_run_bindings(
        [_input_stage("load", str(tmp_path / "a.csv"))],
        {"load": {"path": bound, "format": "parquet"}})
    assert stages[0].connector.params == {"path": bound, "format": "parquet"}


def test_invalid_merged_params_rejected_naming_the_stage(tmp_path):
    # The runner attaches no meaning to params — the Connector model re-validates
    # the merged result (here: a relative path) and the error names the stage.
    with pytest.raises(ValueError, match="load"):
        apply_run_bindings([_input_stage("load", None)], {"load": {"path": "data/items.csv"}})


def test_non_dict_binding_rejected_with_stage_id(tmp_path):
    # A binding is a dict of connector params — a bare path string (the old
    # shorthand) or any other non-dict must fail loudly and name the stage.
    with pytest.raises(ValueError, match="load"):
        apply_run_bindings([_input_stage("load", None)], {"load": str(tmp_path / "b.csv")})


def test_unknown_binding_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="nope"):
        apply_run_bindings(
            [_input_stage("load", str(tmp_path / "a.csv"))],
            {"nope": {"path": str(tmp_path / "b.csv")}})


def test_binding_connectorless_stage_rejected(tmp_path):
    # Bindings override connector params; a stage with no connector has nothing
    # to bind, whatever its type. Generic rule — no file/type special-casing.
    stages = [_input_stage("load", str(tmp_path / "a.csv")),
              _connectorless_stage("derive", "load")]
    with pytest.raises(ValueError, match="derive"):
        apply_run_bindings(stages, {"derive": {"path": str(tmp_path / "b.csv")}})


def test_original_stages_untouched(tmp_path):
    authored = str(tmp_path / "a.csv")
    original = _input_stage("load", authored)
    apply_run_bindings([original], {"load": {"path": str(tmp_path / "b.csv")}})
    assert original.connector.params["path"] == authored


# ── validate_stages_ready: stage-owned preflight, aggregated loudly ────────────

def test_unready_stages_all_named(tmp_path):
    stages = [_input_stage("load_a", None), _input_stage("load_b", None)]
    with pytest.raises(MissingInputBindingError) as exc:
        validate_stages_ready(stages, {"load_a": "workflow", "load_b": "workflow"})
    assert "load_a" in str(exc.value) and "load_b" in str(exc.value)


def test_ready_stage_yields_provenance_record(tmp_path):
    data = tmp_path / "a.csv"
    pd.DataFrame({"x": [1]}).to_csv(data, index=False)
    records = validate_stages_ready(
        [_input_stage("load", str(data))], {"load": "workflow"})
    assert records["load"]["path"] == str(data)
    assert records["load"]["source"] == "workflow"
    assert records["load"]["sha256"] == hashlib.sha256(data.read_bytes()).hexdigest()
    assert records["load"]["bytes"] == data.stat().st_size


def test_connectorless_stage_has_no_preflight(tmp_path):
    data = tmp_path / "a.csv"
    pd.DataFrame({"x": [1]}).to_csv(data, index=False)
    stages = [_input_stage("load", str(data)), _connectorless_stage("derive", "load")]
    records = validate_stages_ready(stages, {"load": "workflow"})
    assert set(records) == {"load"}


# ── prepare_run integration ─────────────────────────────────────────────────

def _make_bound_project(root, filename="a.csv"):
    (root / "compiled").mkdir(parents=True)
    data = root / filename
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    stage = {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    vid = create_version_from_disk(root, message="seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")
    return data


def test_run_binding_recorded_with_hash_and_source(tmp_path):
    _make_bound_project(tmp_path)
    other = tmp_path / "b.csv"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_csv(other, index=False)

    manifest = execute_run(tmp_path, repo_root=tmp_path,
                           bindings={"load": {"path": str(other)}})

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
    vid = create_version_from_disk(tmp_path, message="seed", reviewer="test").version_id
    versioning.publish_version(tmp_path, vid, reviewer="human")

    with pytest.raises(MissingInputBindingError, match="load"):
        execute_run(tmp_path, repo_root=tmp_path)
    assert not (tmp_path / "runs").exists()


def test_bound_file_must_exist_before_run_dir(tmp_path):
    _make_bound_project(tmp_path)
    with pytest.raises(MissingInputBindingError, match="ghost"):
        execute_run(tmp_path, repo_root=tmp_path,
                    bindings={"load": {"path": str(tmp_path / "ghost.csv")}})
    assert not (tmp_path / "runs").exists()


def test_handler_ignores_repo_root_for_file_inputs(tmp_path):
    # The path is absolute; repo_root must play no part in resolving it.
    _make_bound_project(tmp_path)
    elsewhere = tmp_path / "unrelated_repo_root"
    elsewhere.mkdir()
    manifest = execute_run(tmp_path, repo_root=elsewhere)
    assert manifest["status"] == "ok"
    assert manifest["stages"][0]["rows"] == 2


def test_read_input_data_names_the_stage_when_no_path_is_bound(tmp_path):
    # A path-free input reaching the handler directly (run_subset, or any
    # caller that skips prepare_run's preflight) must fail with a message that
    # names the stage and explains why, not a bare KeyError.
    stage = _input_stage("load_lobbying_filings", None)
    with pytest.raises(ValueError, match="load_lobbying_filings"):
        read_input_data(stage, ctx={})
