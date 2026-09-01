from __future__ import annotations


import pandas as pd
import pytest
from fastapi.datastructures import FormData
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services import workspace
from app.services.project import save_working_copy_as_version
from app.web.routers.runs import _read_bust_cache
from stage_seed import add_stage
from run_seed import read_manifest

client = TestClient(app)


def test_a_checked_box_reads_as_busted():
    assert _read_bust_cache(FormData([("bust_cache", "on")])) is True


def test_an_absent_box_reads_as_not_busted():
    assert _read_bust_cache(FormData([("version_id", "v1")])) is False


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}},
             "signature": {
                 "form": "replaces",
                 "produces": [
                     {"name": "name", "type": "str", "nullable": True},
                     {"name": "val", "type": "int", "nullable": True},
                 ],
             }}
    add_stage(proj, stage)
    save_working_copy_as_version(proj.name, message="seed")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _manifest(proj):
    run_dir = sorted((proj / "runs").iterdir())[-1]
    return read_manifest(run_dir.parent.parent, run_dir.name)


def test_checked_box_becomes_a_busted_run(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": "", "bust_cache": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _manifest(project)["parameters"]["bust_cache"] is True


def test_an_unchecked_box_submits_nothing_and_the_run_is_not_busted(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _manifest(project)["parameters"]["bust_cache"] is False


def test_new_run_page_offers_the_checkbox(project):
    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert 'name="bust_cache"' in resp.text


def test_the_checkbox_sits_inside_the_advanced_fold(project):
    body = client.get("/project/demo/runs/new").text
    # The fold is closed on load (no `open`), and the checkbox is inside it — so the
    # default run reuses cached rows without the reader deciding anything.
    fold = body.split('<details class="run-advanced">')[1].split("</details>")[0]
    assert 'name="bust_cache"' in fold
    assert '<details class="run-advanced" open>' not in body
