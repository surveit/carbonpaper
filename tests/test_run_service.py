"""The production run service seam (app.services.run): start a run on a
background thread, read a run's manifest status, resume, and resolve the pinned
version. Builds a small file-connector project, snapshots + publishes it into a
version (the same idiom as tests/test_runner.py), and drives the service
directly. The background launch is monkeypatched to run synchronously so each
test can assert on the finished manifest deterministically."""
from __future__ import annotations

import json

import pandas as pd
import pytest

import app.services.run as run_service
from app.core.errors import NoVersionToRunError, RunNotFoundError
from app.services import versioning
from app.services.versioning import create_version_from_disk, list_versions


@pytest.fixture(autouse=True)
def _synchronous_background(monkeypatch):
    """Run the service's background launch inline so a test can assert on the
    finished manifest without racing a daemon thread."""
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))


def _make_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(
        root / "data" / "items.csv", index=False)
    stage = {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(root / "data" / "items.csv"),
                                 "format": "csv"}},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def _seed_version(root):
    vid = create_version_from_disk(root, message="seed", reviewer="test").version_id
    versioning.publish_version(root, vid, reviewer="human")
    return vid


def test_start_run_returns_run_id_and_writes_ok_manifest(tmp_path):
    """start_run mints a run id, executes the pinned version, and the on-disk
    manifest reflects the finished run."""
    _make_project(tmp_path)
    _seed_version(tmp_path)
    run_id = run_service.start_run(tmp_path, tmp_path)
    manifest_path = tmp_path / "runs" / run_id / "manifest.json"
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "ok"


def test_start_run_pins_requested_version(tmp_path):
    """A version_id passed to start_run is the version recorded on the run."""
    _make_project(tmp_path)
    vid = _seed_version(tmp_path)
    run_id = run_service.start_run(tmp_path, tmp_path, version_id=vid)
    status = run_service.read_run_status(tmp_path, run_id)
    assert status["workflow_version"] == vid


def test_read_run_status_returns_manifest_dict(tmp_path):
    """read_run_status returns the started run's manifest as a dict."""
    _make_project(tmp_path)
    _seed_version(tmp_path)
    run_id = run_service.start_run(tmp_path, tmp_path)
    status = run_service.read_run_status(tmp_path, run_id)
    assert status["run_id"] == run_id
    assert status["status"] == "ok"


def test_read_run_status_missing_run_raises(tmp_path):
    """A run id with no manifest fails loudly, not with an empty/fabricated status."""
    _make_project(tmp_path)
    with pytest.raises(RunNotFoundError):
        run_service.read_run_status(tmp_path, "20990101T000000")


def test_resolve_version_defaults_to_latest_published_and_raises_when_none(tmp_path):
    """resolve_version(None) returns the newest published version; a project with
    no published version raises NoVersionToRunError."""
    _make_project(tmp_path)
    with pytest.raises(NoVersionToRunError):
        run_service.resolve_version(tmp_path, None)
    vid = _seed_version(tmp_path)
    assert run_service.resolve_version(tmp_path, None) == vid
    assert list_versions(tmp_path)[0].version_id == vid
