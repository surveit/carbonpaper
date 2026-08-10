from __future__ import annotations

import json

import pandas as pd
import pytest

import app.services.run as run_service
from app.core.errors import NoVersionToRunError, RunNotFoundError
from app.services import workspace
from app.services.project import save_working_copy_as_version
from app.services.versioning import list_versions

# The run service takes a project NAME and resolves it under the workspace root;
# every test drives that one project.
_PROJECT = "proj"


@pytest.fixture(autouse=True)
def _synchronous_background(monkeypatch):
    """Runs the launch inline so a test can assert on the manifest without racing a daemon."""
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    workspace.set_projects_dir(tmp_path)
    return tmp_path / _PROJECT


def _make_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"),
                                 "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "name", "type": "str", "nullable": True},
                {"name": "val", "type": "int", "nullable": True},
            ],
        },
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def _seed_version(root):
    return save_working_copy_as_version(root, message="seed", reviewer="test").version_id


def test_start_run_returns_run_id_and_writes_ok_manifest(project_dir):
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)
    manifest_path = project_dir / "runs" / run_id / "manifest.json"
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "ok"


def test_start_run_pins_requested_version(project_dir):
    _make_project(project_dir)
    vid = _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT, version_id=vid)
    status = run_service.read_run_status(_PROJECT, run_id)
    assert status["workflow_version"] == vid


def test_read_run_status_returns_manifest_dict(project_dir):
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)
    status = run_service.read_run_status(_PROJECT, run_id)
    assert status["run_id"] == run_id
    assert status["status"] == "ok"


def test_read_run_status_missing_run_raises(project_dir):
    _make_project(project_dir)
    with pytest.raises(RunNotFoundError):
        run_service.read_run_status(_PROJECT, "20990101T000000")


def test_resolve_version_defaults_to_latest_stored_and_raises_when_none(project_dir):
    _make_project(project_dir)
    with pytest.raises(NoVersionToRunError):
        run_service.resolve_version(_PROJECT, None)
    vid = _seed_version(project_dir)  # never published
    assert run_service.resolve_version(_PROJECT, None) == vid
    assert list_versions(project_dir)[0].version_id == vid


# ─── wait_for_run_to_finish: one blocking call, never a fabricated failure ────

def _running_manifest(stage_status: str = "running"):
    return {
        "run_id": "20260810T101112",
        "status": "running",
        "stage_records": [
            {"stage_id": "load", "status": stage_status, "output_row_count": 0}
        ],
    }


def test_wait_for_run_to_finish_returns_the_terminal_status_and_stage_counts(project_dir):
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)

    outcome = run_service.wait_for_run_to_finish(
        _PROJECT, run_id, timeout_seconds=5, poll_seconds=0.01
    )

    assert (outcome.run_id, outcome.status, outcome.is_terminal) == (run_id, "ok", True)
    assert [(s.stage_id, s.status, s.output_row_count) for s in outcome.stages] == [
        ("load", "ok", 2)
    ]


def test_wait_for_run_to_finish_keeps_waiting_until_the_run_settles(monkeypatch):
    reads = iter([_running_manifest(), _running_manifest(), {
        "run_id": "r", "status": "ok",
        "stage_records": [{"stage_id": "load", "status": "ok", "output_row_count": 2}],
    }])
    monkeypatch.setattr(run_service, "read_run_status", lambda p, r: next(reads))

    outcome = run_service.wait_for_run_to_finish(
        _PROJECT, "r", timeout_seconds=5, poll_seconds=0.001
    )

    assert (outcome.status, outcome.is_terminal) == ("ok", True)


def test_a_deadline_reports_what_the_run_is_doing_and_never_claims_failure(monkeypatch):
    monkeypatch.setattr(
        run_service, "read_run_status", lambda p, r: _running_manifest()
    )

    outcome = run_service.wait_for_run_to_finish(
        _PROJECT, "r", timeout_seconds=0.05, poll_seconds=0.001
    )

    assert outcome.is_terminal is False
    assert outcome.status == "running"
    # Not "errors", not "cancelled", and no invented error text: the stage record says
    # which stage is still going, which is what a caller reports instead of a failure.
    assert [(s.stage_id, s.status, s.error) for s in outcome.stages] == [
        ("load", "running", None)
    ]


def test_wait_for_run_to_finish_carries_a_failed_stages_own_error_message(monkeypatch):
    monkeypatch.setattr(run_service, "read_run_status", lambda p, r: {
        "run_id": "r", "status": "errors",
        "stage_records": [{
            "stage_id": "load", "status": "error", "output_row_count": 0,
            "error": {"type": "ValueError", "message": "no such column", "traceback": None},
        }],
    })

    outcome = run_service.wait_for_run_to_finish(
        _PROJECT, "r", timeout_seconds=1, poll_seconds=0.001
    )

    assert outcome.is_terminal is True
    assert outcome.stages[0].error == "no such column"


def test_wait_for_run_to_finish_raises_on_an_unknown_run(project_dir):
    _make_project(project_dir)
    with pytest.raises(RunNotFoundError):
        run_service.wait_for_run_to_finish(
            _PROJECT, "20990101T000000", timeout_seconds=1, poll_seconds=0.001
        )
