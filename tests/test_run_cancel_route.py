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

from app.main import app
from app.runtime.cancellation import consume_cancel
from app.services import workspace

PROJ = "testmeth"
RUN = "run-0001"


@pytest.fixture()
def examples_dir(tmp_path: Path, monkeypatch) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _write_manifest(examples_dir: Path, status: str) -> Path:
    run_dir = examples_dir / PROJ / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"run_id": RUN, "started_at": RUN, "project": PROJ,
                    "workflow_version": RUN, "status": status,
                    "human_review_queue_stats": {}, "stage_records": []}),
        encoding="utf-8",
    )
    return run_dir


def test_cancel_on_a_running_run_requests_cancellation_and_redirects(examples_dir, client):
    _write_manifest(examples_dir, "running")
    r = client.post(f"/project/{PROJ}/runs/{RUN}/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/project/{PROJ}/runs/{RUN}"
    assert consume_cancel(PROJ, RUN) is True  # the route dropped a cancel message


def test_cancel_on_a_terminal_run_is_a_noop_but_still_redirects(examples_dir, client):
    _write_manifest(examples_dir, "ok")
    r = client.post(f"/project/{PROJ}/runs/{RUN}/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/project/{PROJ}/runs/{RUN}"
    assert consume_cancel(PROJ, RUN) is False  # terminal run: no message dropped


def test_cancel_on_a_missing_run_404s(examples_dir, client):
    r = client.post(f"/project/{PROJ}/runs/no-such-run/cancel")
    assert r.status_code == 404


def _write_one_stage_project(examples_dir: Path) -> None:
    proj_dir = examples_dir / PROJ
    (proj_dir / "compiled").mkdir(parents=True)
    (proj_dir / "data").mkdir(parents=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(proj_dir / "data" / "items.csv", index=False)
    stage = {"id": "load", "description": "Load items", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(proj_dir / "data" / "items.csv"), "format": "csv"}}}
    (proj_dir / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def _write_status_manifest(examples_dir: Path, stage_statuses: list[tuple[str, str]]) -> Path:
    """Write a manifest whose stages carry the given (stage_id, status) pairs,
    for exercising run_status's per-status counts."""
    run_dir = examples_dir / PROJ / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    stages: list[dict[str, object]] = [
        {"stage_id": sid, "type": "input_data", "description": sid, "status": status,
         "input_validation_report": [], "output_validation_report": None,
         "output_row_count": 0}
        for sid, status in stage_statuses]
    (run_dir / "manifest.json").write_text(
        json.dumps({"run_id": RUN, "started_at": RUN, "project": PROJ,
                    "workflow_version": RUN, "status": "cancelled",
                    "human_review_queue_stats": {}, "stage_records": stages}),
        encoding="utf-8",
    )
    return run_dir


def test_run_status_counts_include_a_cancelled_stage(examples_dir, client):
    """A stage cancelled mid-fan-out (runner's `except RunCancelled` branch,
    app/runtime/runner.py) must be counted, not silently dropped from every
    bucket — the run page's stage strip counts the same seven statuses this
    `counts` map does."""
    _write_one_stage_project(examples_dir)
    _write_status_manifest(examples_dir, [
        ("load", "ok"),
        ("score", "cancelled"),
        ("publish", "pending"),
    ])

    resp = client.get(f"/project/{PROJ}/runs/{RUN}/status")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts["cancelled"] == 1
    assert counts["total"] == 3
    assert counts["ok"] == 1
    assert counts["pending"] == 1


def test_run_detail_page_offers_resume_for_a_cancelled_run(examples_dir, client):
    """POST /resume works on a cancelled run (it re-runs every not-complete
    stage and reuses completed outputs), so the run page must offer it —
    the resume bar is gated on errored stages OR a cancelled run, and a
    cancelled run has no errored stages."""
    _write_one_stage_project(examples_dir)
    _write_status_manifest(examples_dir, [
        ("load", "ok"),
        ("score", "cancelled"),
        ("publish", "pending"),
    ])

    page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert page.status_code == 200
    assert f'action="/project/{PROJ}/runs/{RUN}/resume"' in page.text
    assert "Resume cancelled run" in page.text
    # The errored-run wording stays reserved for runs with failed stages.
    assert "Re-run failed stage" not in page.text


def test_run_detail_page_hides_resume_for_a_completed_run(examples_dir, client):
    _write_one_stage_project(examples_dir)
    _write_manifest(examples_dir, "ok")

    page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert page.status_code == 200
    assert f'action="/project/{PROJ}/runs/{RUN}/resume"' not in page.text


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
