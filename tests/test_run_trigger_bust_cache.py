"""POST /project/{name}/run with the `bust_cache` checkbox field, and the
`_collect_bust_cache` helper that reads it.

`bust_cache` is the run's own "recompute everything" flag (RunContext.
bust_cache — see app/runtime/context.py): `prepare_run` already supports it
end-to-end (manifest key `bust_cache`, threaded to RunContext.for_production,
replayed on resume — see app/runtime/runner.py). This file only covers the
web-form surface that reads the `bust_cache` checkbox and threads it into
`run_service.start_run`; the runner's own bust_cache semantics are covered by
tests/runtime/test_llm_cache.py and tests/runtime/test_hrq_cache.py.
"""
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
from app.web.routers.runs import _collect_bust_cache

client = TestClient(app)


# ── _collect_bust_cache: pure-function parsing of the bust_cache field ─────

def test_collects_true_when_checkbox_present():
    form = FormData([("bust_cache", "1")])
    assert _collect_bust_cache(form) is True


def test_collects_false_when_checkbox_absent():
    form = FormData([("version_id", "v1")])
    assert _collect_bust_cache(form) is False


def test_collects_false_when_field_present_but_blank():
    form = FormData([("bust_cache", "")])
    assert _collect_bust_cache(form) is False


# ── Web integration: POST /project/<name>/run with bust_cache ──────────────

@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    (proj / "compiled").mkdir(parents=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y", "z"], "val": [1, 2, 3]}).to_csv(data, index=False)
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


def test_run_form_bust_cache_checkbox_becomes_a_manifest_flag(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": str(project / "a.csv"), "bust_cache": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _manifest(project)["bust_cache"] is True


def test_run_form_without_the_checkbox_leaves_bust_cache_false(project):
    resp = client.post(
        "/project/demo/run",
        data={"binding__load": str(project / "a.csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _manifest(project)["bust_cache"] is False


def test_runs_page_shows_the_bust_cache_checkbox(project):
    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert 'name="bust_cache"' in resp.text
