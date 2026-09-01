"""Guards the enum.StrEnum rendering guarantee: swapping it for `class X(str, Enum)`
(the pattern app.models uses) would break every assertion here.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.run_status import RunStatus, StageStatus
from app.main import app
from app.runtime.runner import execute_run
from app.services.project import save_working_copy_as_version
from app.services import workspace
from conftest import pinned_stages
from stage_seed import add_stage
from run_seed import manifest_text

# The exact value sets, collected by grepping every `record["status"]` /
# `manifest["status"]` literal the runner writes (app/runtime/runner.py) and
# the status_glyph/status_stroke maps that key off them (app/web/diagrams.py).
STAGE_STATUS_VALUES = {
    "pending", "running", "ok", "validation_warnings",
    "error", "awaiting_review", "cancelled",
}
RUN_STATUS_VALUES = {
    "running", "ok", "warnings", "errors", "awaiting_review", "cancelled",
}


@pytest.mark.parametrize("member", list(StageStatus), ids=lambda m: m.name)
def test_stage_status_member_renders_as_its_bare_value(member: StageStatus) -> None:
    assert isinstance(member, str)
    assert str(member) == member.value
    assert f"{member}" == member.value
    assert json.dumps(member) == json.dumps(member.value)
    assert member == member.value


@pytest.mark.parametrize("member", list(RunStatus), ids=lambda m: m.name)
def test_run_status_member_renders_as_its_bare_value(member: RunStatus) -> None:
    assert isinstance(member, str)
    assert str(member) == member.value
    assert f"{member}" == member.value
    assert json.dumps(member) == json.dumps(member.value)
    assert member == member.value


def test_stage_status_matches_the_values_the_runner_produces() -> None:
    assert {m.value for m in StageStatus} == STAGE_STATUS_VALUES


def test_run_status_matches_the_values_the_runner_produces() -> None:
    assert {m.value for m in RunStatus} == RUN_STATUS_VALUES


def test_css_class_pattern_renders_bare_not_qualified() -> None:
    # The pattern _stage_strip.html uses: `class="status-{{ square.status }}"`.
    assert f"status-{RunStatus.OK}" == "status-ok"
    assert f"status-{StageStatus.VALIDATION_WARNINGS}" == "status-validation_warnings"


# ─── End-to-end: the real producer (app.runtime.runner) through to disk ──────

PROJECT = "status_enum_journey"


def _make_project(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"), "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "name", "type": "str", "nullable": True},
                {"name": "val", "type": "int", "nullable": True},
            ],
        },
    }
    add_stage(root, stage)


def _seed_and_publish(project_dir) -> None:
    save_working_copy_as_version(project_dir.name, message="seed")


def test_a_real_run_produces_enum_statuses_that_round_trip_to_bare_strings(tmp_path) -> None:
    _make_project(tmp_path)
    _seed_and_publish(tmp_path)

    manifest = execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))

    # The producer's in-memory manifest carries real enum members, not plain
    # str — and they still equal / stringify as the bare value.
    assert manifest["status"] == RunStatus.OK
    assert isinstance(manifest["status"], RunStatus)
    assert str(manifest["status"]) == "ok"
    assert manifest["stage_records"][0]["status"] == StageStatus.OK
    assert isinstance(manifest["stage_records"][0]["status"], StageStatus)

    # Stored — what templates/JS actually read — it round-trips to a bare
    # JSON string, with no trace of the enum class name.
    raw_text = manifest_text(tmp_path, manifest["run_id"])
    on_disk = json.loads(raw_text)
    assert on_disk["status"] == "ok"
    assert on_disk["stage_records"][0]["status"] == "ok"
    assert "RunStatus" not in raw_text
    assert "StageStatus" not in raw_text


def test_a_real_run_renders_bare_status_through_the_web_layer(tmp_path, monkeypatch) -> None:
    workspace.set_projects_dir(tmp_path)
    project_dir = tmp_path / PROJECT
    _make_project(project_dir)
    _seed_and_publish(project_dir)

    manifest = execute_run(project_dir / "runs", project_dir.name, *pinned_stages(project_dir))
    run_id = manifest["run_id"]

    client = TestClient(app)

    page = client.get(f"/project/{PROJECT}/runs/{run_id}")
    assert page.status_code == 200
    assert "status-ok" in page.text
    assert "RunStatus" not in page.text

    status = client.get(f"/project/{PROJECT}/runs/{run_id}/status").json()
    assert status["status"] == "ok"
    assert status["counts"]["ok"] == 1
    assert status["counts"]["total"] == 1
