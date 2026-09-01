from __future__ import annotations


import pandas as pd
import pytest

import app.services.run as run_service
from app.core.errors import NoVersionToRunError, RunNotFoundError
from app.services import workspace
from app.services.project import save_working_copy_as_version
from app.services.versioning import list_versions
from stage_seed import add_stage
from run_seed import manifest_exists, read_manifest

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
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
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
    add_stage(root, stage)


def _seed_version(root):
    return save_working_copy_as_version(root.name, message="seed").version_id


def test_start_run_returns_run_id_and_writes_ok_manifest(project_dir):
    _make_project(project_dir)
    _seed_version(project_dir)
    run_id = run_service.start_run(_PROJECT)
    manifest_project = project_dir

    manifest_run = run_id
    assert manifest_exists(manifest_project, manifest_run)
    assert read_manifest(manifest_project, manifest_run)["status"] == "ok"


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
    assert list_versions(project_dir.name)[0].version_id == vid
