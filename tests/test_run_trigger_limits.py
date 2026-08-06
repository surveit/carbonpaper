from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.datastructures import FormData
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services import versioning
from app.services import workspace
from app.services.project import save_working_copy_as_version
from app.web.routers.runs import _collect_limits

client = TestClient(app)


# ── _collect_limits: pure-function parsing of limit__<stage_id> fields ─────

def test_collects_integer_limits_keyed_by_stage_id():
    form = FormData([("limit__load", "5"), ("limit__score", "10")])
    assert _collect_limits(form) == {"load": 5, "score": 10}


def test_blank_limit_is_skipped_not_recorded_as_zero():
    form = FormData([("limit__load", ""), ("limit__score", "   ")])
    assert _collect_limits(form) == {}


def test_ignores_form_fields_that_are_not_limit_fields():
    form = FormData([("version_id", "v1"), ("binding__load", "/a.csv"), ("limit__load", "3")])
    assert _collect_limits(form) == {"load": 3}


def test_non_integer_limit_raises_naming_the_stage():
    form = FormData([("limit__load", "abc")])
    with pytest.raises(ValueError, match="load"):
        _collect_limits(form)


def test_negative_limit_raises_naming_the_stage():
    form = FormData([("limit__load", "-1")])
    with pytest.raises(ValueError, match="load"):
        _collect_limits(form)


def test_fractional_limit_raises():
    form = FormData([("limit__load", "1.5")])
    with pytest.raises(ValueError, match="load"):
        _collect_limits(form)


# ── Web integration: POST /project/<name>/run with limit__ fields ──────────

@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    (proj / "compiled").mkdir(parents=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y", "z"], "val": [1, 2, 3]}).to_csv(data, index=False)
    # output_schema names the CSV's columns; every non-publish stage must declare
    # one (app/models/stage.py: Stage._schemas_declared).
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "signature": {
                 "form": "replaces",
                 "produces": [
                     {"name": "name", "type": "str", "nullable": False},
                     {"name": "val", "type": "int", "nullable": False},
                 ],
             },
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    (proj / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    vid = save_working_copy_as_version(proj, message="seed", reviewer="test").version_id
    versioning.publish_version(proj, vid, reviewer="human")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _manifest(proj):
    run_dir = sorted((proj / "runs").iterdir())[-1]
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_run_form_limit_field_becomes_a_manifest_limit_override(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": str(project / "a.csv"), "limit__load": "2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _manifest(project)["parameters"]["limits"] == {"load": 2}


def test_blank_limit_field_records_no_override(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": str(project / "a.csv"), "limit__load": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _manifest(project)["parameters"]["limits"] == {}


def test_non_integer_limit_returns_400_and_creates_no_run(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": str(project / "a.csv"), "limit__load": "abc"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "load" in resp.json()["detail"]
    assert not (project / "runs").exists()


def test_negative_limit_returns_400_and_creates_no_run(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": str(project / "a.csv"), "limit__load": "-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "load" in resp.json()["detail"]
    assert not (project / "runs").exists()


def test_runs_page_shows_a_limit_field_per_file_input(project):
    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert 'name="limit__load"' in resp.text
