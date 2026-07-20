"""POST /project/{project}/runs/{run_id}/cancel — cooperative cancel of a
running run, and the run-detail page's Cancel button. See
app/runtime/cancellation.py for the request/poll design this route drives.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.loading as loading
from app.main import app
from app.runtime.cancellation import clear, is_cancelled

PROJ = "testmeth"
RUN = "run-0001"


@pytest.fixture()
def examples_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _write_manifest(examples_dir: Path, status: str) -> Path:
    run_dir = examples_dir / PROJ / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"run_id": RUN, "status": status, "stages": []}), encoding="utf-8"
    )
    return run_dir


def test_cancel_on_a_running_run_requests_cancellation_and_redirects(examples_dir, client):
    _write_manifest(examples_dir, "running")
    try:
        r = client.post(f"/project/{PROJ}/runs/{RUN}/cancel", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/project/{PROJ}/runs/{RUN}"
        assert is_cancelled(PROJ, RUN) is True
    finally:
        clear(PROJ, RUN)


def test_cancel_on_a_terminal_run_is_a_noop_but_still_redirects(examples_dir, client):
    _write_manifest(examples_dir, "ok")
    r = client.post(f"/project/{PROJ}/runs/{RUN}/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/project/{PROJ}/runs/{RUN}"
    assert is_cancelled(PROJ, RUN) is False


def test_cancel_on_a_missing_run_404s(examples_dir, client):
    r = client.post(f"/project/{PROJ}/runs/no-such-run/cancel")
    assert r.status_code == 404


def _write_one_stage_project(examples_dir: Path) -> None:
    proj_dir = examples_dir / PROJ
    (proj_dir / "compiled").mkdir(parents=True)
    (proj_dir / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(proj_dir / "data" / "items.csv", index=False)
    stage = {"id": "load", "name": "Load items", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(proj_dir / "data" / "items.csv"), "format": "csv"}}}
    (proj_dir / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def test_run_detail_page_shows_cancel_button_only_while_running(examples_dir, client):
    _write_one_stage_project(examples_dir)
    _write_manifest(examples_dir, "running")

    running_page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert running_page.status_code == 200
    assert f'action="/project/{PROJ}/runs/{RUN}/cancel"' in running_page.text

    _write_manifest(examples_dir, "ok")
    done_page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert done_page.status_code == 200
    assert f'action="/project/{PROJ}/runs/{RUN}/cancel"' not in done_page.text
