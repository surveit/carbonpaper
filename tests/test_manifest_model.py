"""Goldens in tests/goldens/*.json are real manifests: the three named for a run
state are pre-typing and carry their parameters FLAT, `ok_run_nested_parameters`
is the current nested shape. Byte-for-byte identity only holds for a settled
manifest already in the current shape — rewriting a legacy one migrates it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.run_status import RunStatus, StageStatus
from app.models import parse_stage
from app.models.stage_contribution import StageContribution
from app.runtime.manifest import RunManifest
from app.runtime.context import RunContext
from app.runtime.manifest import create_run_manifest
from conftest import place_stage

GOLDENS = Path(__file__).parent / "goldens"


def _golden(name: str) -> str:
    return (GOLDENS / f"{name}.json").read_text(encoding="utf-8")


def _reserialize(raw: str) -> str:
    return json.dumps(RunManifest.model_validate_json(raw).to_dict(), indent=2, default=str)


def test_a_current_shape_manifest_round_trips_byte_identical():
    raw = _golden("ok_run_nested_parameters")
    assert _reserialize(raw) == raw


def test_a_legacy_manifest_keeps_every_recorded_parameter():
    legacy = json.loads(_golden("ok_run"))
    legacy |= {"limit_overrides": {"load": 2}, "offset_overrides": {"load": 1},
               "bust_cache": True, "is_test_run": True}
    parameters = RunManifest.model_validate_json(json.dumps(legacy)).parameters
    assert parameters.limits == {"load": 2} and parameters.offsets == {"load": 1}
    assert parameters.bust_cache is True and parameters.is_test_run is True


def test_rewriting_a_legacy_manifest_migrates_it_to_the_nested_shape():
    rewritten = json.loads(_reserialize(_golden("ok_run")))
    assert rewritten["parameters"] == {"limits": {}, "offsets": {}, "run_bindings": {}}
    for gone in ("limit_overrides", "offset_overrides", "run_bindings", "bust_cache"):
        assert gone not in rewritten


@pytest.mark.parametrize("name", ["ok_run", "errored_run", "halted_run"])
def test_a_legacy_manifest_round_trips_structurally(name: str):
    raw = _golden(name)
    # Compared through `to_dict`, which is what a manifest RECORDED — the store's
    # own id/created_at/updated_at are minted per parse and are not part of it.
    assert RunManifest.model_validate_json(_reserialize(raw)).to_dict() == (
        RunManifest.model_validate_json(raw).to_dict())


def test_a_stage_record_carries_only_the_optionals_it_earned():
    halted = RunManifest.model_validate_json(_golden("halted_run"))
    by_id = {r.stage_id: r for r in halted.stage_records}

    load = by_id["load"].model_dump(exclude_unset=True)
    assert "output_path" in load and "queue_path" not in load and "llm_usage" not in load

    review = by_id["review"].model_dump(exclude_unset=True)
    assert "queue_path" in review and "output_path" not in review

    tail = by_id["tail"].model_dump(exclude_unset=True)
    assert not ({"output_path", "queue_path", "notes", "llm_usage"} & set(tail))


def test_minted_manifest_omits_the_run_level_optionals():
    abs_path = str((Path.cwd() / "x.csv").resolve())
    stage = parse_stage(
        {"id": "s", "description": "S", "type": "input_data",
         "connector": {"kind": "file", "params": {"path": abs_path, "format": "csv"}},
         "signature": {
             "form": "replaces",
             "produces": [{"name": "k", "type": "str", "nullable": True}],
         }}
    )
    manifest = create_run_manifest(
        [place_stage(stage)], RunContext(run_dir=None),
        run_id="r", project_id="p", workflow_version="v",
        input_bindings={})
    dumped = manifest.to_dict()

    assert dumped["status"] == RunStatus.RUNNING
    assert dumped["stage_records"][0]["status"] == StageStatus.PENDING
    for absent in ("finished_at", "halted_at", "cancelled_at", "resumed_at", "updated_at"):
        assert absent not in dumped
    # The always-present core fields ARE emitted even when empty.
    for present in ("human_review_queue_stats", "dropped_columns", "parameters"):
        assert present in dumped


def test_legacy_scalar_halted_at_is_normalized_to_a_list():
    """A template iterating a bare string would walk it character by character."""
    raw = json.loads(_golden("halted_run"))
    raw["halted_at"] = "review"
    manifest = RunManifest.model_validate(raw)
    assert manifest.halted_at == ["review"]
    assert manifest.to_dict()["halted_at"] == ["review"]


def test_clear_halt_drops_halted_at_from_serialization():
    manifest = RunManifest.model_validate_json(_golden("halted_run"))
    assert "halted_at" in manifest.to_dict()
    manifest.clear_halt()
    assert "halted_at" not in manifest.to_dict()


def test_recorded_tallies_survive_serialization_on_a_partial_manifest():
    """`exclude_unset` drops an in-place mutation; `record_dropped_columns` marks the field set."""
    manifest = RunManifest(
        run_id="r", started_at="t", project="p", workflow_version="v",
        status=RunStatus.RUNNING, human_review_queue_stats={}, stage_records=[])
    # dropped_columns defaulted, NOT in the set-fields yet.
    assert "dropped_columns" not in manifest.to_dict()
    manifest.record_dropped_columns("classify", ["scratch"])
    assert manifest.to_dict()["dropped_columns"] == {"classify": ["scratch"]}


def test_a_pre_rename_manifest_fails_loudly_instead_of_reporting_zero():
    legacy = json.loads(_golden("halted_run"))
    legacy["queue_stats"] = legacy.pop("human_review_queue_stats")
    legacy["stages"] = [
        {("rows" if k == "output_row_count" else
          "input_validation" if k == "input_validation_report" else
          "output_validation" if k == "output_validation_report" else k): v
         for k, v in record.items()}
        for record in legacy.pop("stage_records")
    ]
    with pytest.raises(ValidationError):
        RunManifest.model_validate(legacy)


def test_empty_contribution_is_the_default():
    empty = StageContribution()
    assert empty.llm_usage is None and empty.human_review_queue_stats is None
    assert empty.row_errors == [] and empty.dropped_columns == []
