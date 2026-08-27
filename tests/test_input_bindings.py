from __future__ import annotations

import hashlib

import pandas as pd
import pytest

from app.core.errors import MissingInputBindingError
from app.models import Workflow, parse_stage, Stage
from app.runtime.runner import validate_stages_ready, execute_run
from app.runtime.stages.input_data import read_input_data
from app.services import versioning
from app.services.project import save_working_copy_as_version
from conftest import make_run_context, pinned_stages, place_stage
from stage_seed import add_stage


# The one column the file-writing tests here create; Stage._schemas_declared wants it.
_X_SCHEMA = {"columns": [{"name": "x", "type": "int", "nullable": True}]}


def _input_stage(stage_id: str, path: str | None) -> Stage:
    params: dict = {"paths": [path], "format": "csv"} if path else {}
    return parse_stage({
        "id": stage_id, "description": stage_id, "type": "input_data",
        "connector": {"kind": "file", "params": params},
        "signature": {"form": "replaces", "produces": _X_SCHEMA["columns"]},
    })


def _connectorless_stage(stage_id: str, input_id: str) -> Stage:
    return parse_stage({
        "id": stage_id, "description": stage_id, "type": "python_row_function",
        "inputs": [{"id": input_id}],
        "signature": {"form": "extends"},
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    })


def apply_run_bindings(stages: list[Stage], bindings) -> tuple[list[Stage], dict[str, str]]:
    """Test shim: the app binds through a Workflow; these cases author bare stage lists."""
    workflow, sources = Workflow(stages=stages).apply_run_bindings(bindings)
    return workflow.stages, sources


def _ready(stages: list[Stage], sources: dict[str, str]):
    return validate_stages_ready(Workflow(stages=stages).list_workflow_stages(), sources)


# ── Workflow.apply_run_bindings: generic param overrides, no file knowledge ──

def test_run_binding_overrides_workflow_params(tmp_path):
    authored, bound = str(tmp_path / "a.csv"), str(tmp_path / "b.csv")
    stages, sources = apply_run_bindings(
        [_input_stage("load", authored)], {"load": {"path": bound}})
    assert stages[0].connector.params.paths == [bound]
    assert sources == {"load": "run"}


def test_workflow_params_used_when_no_binding(tmp_path):
    authored = str(tmp_path / "a.csv")
    stages, sources = apply_run_bindings([_input_stage("load", authored)], None)
    assert sources == {"load": "workflow"}
    assert stages[0].connector.params.paths[0] == authored


def test_binding_merges_over_params(tmp_path):
    bound = str(tmp_path / "b.parquet")
    stages, _ = apply_run_bindings(
        [_input_stage("load", str(tmp_path / "a.csv"))],
        {"load": {"paths": [bound], "format": "parquet"}})
    assert stages[0].connector.params.paths == [bound]
    assert stages[0].connector.params.format == "parquet"


def test_a_rerun_replaying_an_old_runs_binding_still_names_its_file(tmp_path):
    """A manifest written before an input could read several files recorded `path`."""
    bound = str(tmp_path / "b.parquet")
    stages, _ = apply_run_bindings(
        [_input_stage("load", str(tmp_path / "a.csv"))],
        {"load": {"path": bound, "format": "parquet"}})
    assert stages[0].connector.params.paths == [bound]


def test_invalid_merged_params_rejected_naming_the_stage(tmp_path):
    # Connector re-validates the merged result; the relative path below is what fails.
    with pytest.raises(ValueError, match="load"):
        apply_run_bindings([_input_stage("load", None)], {"load": {"path": "data/items.csv"}})


def test_non_dict_binding_rejected_with_stage_id(tmp_path):
    with pytest.raises(ValueError, match="load"):
        apply_run_bindings([_input_stage("load", None)], {"load": str(tmp_path / "b.csv")})


def test_unknown_binding_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="nope"):
        apply_run_bindings(
            [_input_stage("load", str(tmp_path / "a.csv"))],
            {"nope": {"path": str(tmp_path / "b.csv")}})


def test_binding_connectorless_stage_rejected(tmp_path):
    stages = [_input_stage("load", str(tmp_path / "a.csv")),
              _connectorless_stage("score", "load")]
    with pytest.raises(ValueError, match="score"):
        apply_run_bindings(stages, {"score": {"path": str(tmp_path / "b.csv")}})


def test_original_stages_untouched(tmp_path):
    authored = str(tmp_path / "a.csv")
    original = _input_stage("load", authored)
    apply_run_bindings([original], {"load": {"path": str(tmp_path / "b.csv")}})
    assert original.connector.params.paths == [authored]


# ── validate_stages_ready: stage-owned preflight, aggregated loudly ────────────

def test_unready_stages_all_named(tmp_path):
    stages = [_input_stage("load_a", None), _input_stage("load_b", None)]
    with pytest.raises(MissingInputBindingError) as exc:
        _ready(stages, {"load_a": "workflow", "load_b": "workflow"})
    assert "load_a" in str(exc.value) and "load_b" in str(exc.value)


def test_ready_stage_yields_provenance_record(tmp_path):
    data = tmp_path / "a.csv"
    pd.DataFrame({"x": [1]}).to_csv(data, index=False)
    records = _ready([_input_stage("load", str(data))], {"load": "workflow"})
    assert records["load"]["files"][0]["path"] == str(data)
    assert records["load"]["source"] == "workflow"
    assert records["load"]["files"][0]["sha256"] == hashlib.sha256(data.read_bytes()).hexdigest()
    assert records["load"]["files"][0]["bytes"] == data.stat().st_size


def test_connectorless_stage_has_no_preflight(tmp_path):
    data = tmp_path / "a.csv"
    pd.DataFrame({"x": [1]}).to_csv(data, index=False)
    stages = [_input_stage("load", str(data)), _connectorless_stage("score", "load")]
    records = _ready(stages, {"load": "workflow"})
    assert set(records) == {"load"}


# ── prepare_run integration ─────────────────────────────────────────────────

_ROWS_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                            {"name": "val", "type": "int", "nullable": True}]}


def _make_bound_project(root, filename="a.csv"):
    root.mkdir(parents=True, exist_ok=True)
    data = root / filename
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    add_stage(root, stage)
    vid = save_working_copy_as_version(root.name, message="seed", reviewer="test").version_id
    versioning.publish_version(root.name, vid, reviewer="human")
    return data


def test_run_binding_recorded_with_hash_and_source(tmp_path):
    _make_bound_project(tmp_path)
    other = tmp_path / "b.csv"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_csv(other, index=False)

    manifest = execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path),
                           bindings={"load": {"path": str(other)}})

    assert manifest["status"] == "ok"
    rec = manifest["input_bindings"]["load"]
    assert rec["files"][0]["path"] == str(other)
    assert rec["source"] == "run"
    assert rec["files"][0]["sha256"] == hashlib.sha256(other.read_bytes()).hexdigest()
    assert rec["files"][0]["bytes"] == other.stat().st_size
    out = pd.read_parquet(tmp_path / "runs" / manifest["run_id"] / "outputs" / "load.parquet")
    assert list(out["val"]) == [9]                     # read the BOUND file


def test_workflow_path_recorded_as_workflow_source(tmp_path):
    data = _make_bound_project(tmp_path)
    manifest = execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))
    rec = manifest["input_bindings"]["load"]
    assert rec["source"] == "workflow"
    assert rec["files"][0]["path"] == str(data)


def test_unbound_input_leaves_no_run_dir(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    # No file is ever bound, so the declared columns are never materialised —
    # the stage declares the shape the rest of this file's data uses.
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
             "connector": {"kind": "file", "params": {}}}
    add_stage(tmp_path, stage)
    vid = save_working_copy_as_version(tmp_path.name, message="seed", reviewer="test").version_id
    versioning.publish_version(tmp_path.name, vid, reviewer="human")

    with pytest.raises(MissingInputBindingError, match="load"):
        execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))
    assert not (tmp_path / "runs").exists()


def test_bound_file_must_exist_before_run_dir(tmp_path):
    _make_bound_project(tmp_path)
    with pytest.raises(MissingInputBindingError, match="ghost"):
        execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path),
                    bindings={"load": {"path": str(tmp_path / "ghost.csv")}})
    assert not (tmp_path / "runs").exists()


def test_read_input_data_names_the_stage_when_no_path_is_bound(tmp_path):
    # The handler reached directly (execute_subset), skipping prepare_run's preflight.
    stage = _input_stage("load_lobbying_filings", None)
    with pytest.raises(ValueError, match="load_lobbying_filings"):
        read_input_data(place_stage(stage), ctx=make_run_context())
