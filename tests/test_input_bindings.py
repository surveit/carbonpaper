"""apply_input_bindings: per-run input bindings merged into just-loaded stages.

Pure-function tests here; prepare_run integration (manifest provenance, sha256,
no-run-dir-on-failure) is in this file too, added by the next task.
"""
from __future__ import annotations

import pytest

from app.core.errors import MissingInputBindingError
from app.core.models import Stage
from app.runtime.runner import apply_input_bindings


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
