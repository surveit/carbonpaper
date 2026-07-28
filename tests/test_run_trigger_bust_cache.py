from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.datastructures import FormData
from fastapi.testclient import TestClient

import app.web.routers.runs as runs_router
import app.services.run as run_service
from app.main import app
from app.services import versioning
from app.services import workspace
from app.services.versioning import create_version_from_disk
from app.web.routers.runs import _read_bust_cache

client = TestClient(app)


def test_a_checked_box_reads_as_busted():
    assert _read_bust_cache(FormData([("bust_cache", "on")])) is True


def test_an_absent_box_reads_as_not_busted():
    assert _read_bust_cache(FormData([("version_id", "v1")])) is False


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    (proj / "compiled").mkdir(parents=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    stage = {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    (proj / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    vid = create_version_from_disk(proj, message="seed", reviewer="test").version_id
    versioning.publish_version(proj, vid, reviewer="human")
    monkeypatch.setattr(runs_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _manifest(proj):
    run_dir = sorted((proj / "runs").iterdir())[-1]
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_checked_box_becomes_a_busted_run(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": str(project / "a.csv"), "bust_cache": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _manifest(project)["bust_cache"] is True


def test_an_unchecked_box_submits_nothing_and_the_run_is_not_busted(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": str(project / "a.csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _manifest(project)["bust_cache"] is False


def test_runs_page_offers_the_checkbox(project):
    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert 'name="bust_cache"' in resp.text
