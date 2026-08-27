"""POST /project/{project}/runs/{run_id}/cancel — cooperative cancel of a
running run, and the run-detail page's Cancel button. See
app/runtime/cancellation.py for the request/poll design this route drives.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.runtime.cancellation import consume_cancel
from app.services import workspace
from stage_seed import add_stage
from run_seed import read_manifest, store_manifest

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
    store_manifest(run_dir.parent.parent, run_dir.name, {"run_id": RUN, "started_at": RUN, "project": PROJ,
                    "workflow_version": RUN, "status": status,
                    "human_review_queue_stats": {}, "stage_records": []})
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
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"name": ["a"], "val": [1]}).to_csv(proj_dir / "data" / "items.csv", index=False)
    stage = {"id": "load", "description": "Load items", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(proj_dir / "data" / "items.csv"), "format": "csv"}}}
    add_stage(proj_dir, stage)


def _write_status_manifest(examples_dir: Path, stage_statuses: list[tuple[str, str]],
                           status: str = "cancelled") -> Path:
    run_dir = examples_dir / PROJ / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    stages: list[dict[str, object]] = [
        {"stage_id": sid, "type": "input_data", "description": sid, "status": status,
         "input_validation_report": [], "output_validation_report": None,
         "output_row_count": 0}
        for sid, status in stage_statuses]
    store_manifest(run_dir.parent.parent, run_dir.name, {"run_id": RUN, "started_at": RUN, "project": PROJ,
                    "workflow_version": RUN, "status": status,
                    "human_review_queue_stats": {}, "stage_records": stages})
    return run_dir


def test_run_status_counts_include_a_cancelled_stage(examples_dir, client):
    _write_one_stage_project(examples_dir)
    _write_status_manifest(examples_dir, [
        ("load", "ok"),
        ("score", "cancelled"),
        ("report", "pending"),
    ])

    resp = client.get(f"/project/{PROJ}/runs/{RUN}/status")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts["cancelled"] == 1
    assert counts["total"] == 3
    assert counts["ok"] == 1
    assert counts["pending"] == 1


def test_run_status_exposes_recorded_stage_progress(examples_dir, client):
    _write_one_stage_project(examples_dir)
    _write_status_manifest(examples_dir, [("load", "running")])
    raw = read_manifest(examples_dir / PROJ, RUN)
    raw["stage_records"][0]["progress"] = {
        "completed": 7,
        "total": 10,
        "updated_at": "2026-08-19T12:00:00",
    }
    store_manifest(examples_dir / PROJ, RUN, raw)

    response = client.get(f"/project/{PROJ}/runs/{RUN}/status")

    assert response.status_code == 200
    assert response.json()["stages"] == [{
        "stage_id": "load",
        "status": "running",
        "progress": {
            "completed": 7,
            "total": 10,
            "updated_at": "2026-08-19T12:00:00",
        },
    }]


def test_run_detail_page_offers_resume_for_a_cancelled_run(examples_dir, client):
    _write_one_stage_project(examples_dir)
    _write_status_manifest(examples_dir, [
        ("load", "ok"),
        ("score", "cancelled"),
        ("report", "pending"),
    ])

    page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert page.status_code == 200
    assert f'action="/project/{PROJ}/runs/{RUN}/resume"' in page.text
    assert "Resume cancelled run" in page.text
    # The errored-run wording stays reserved for runs with failed stages.
    assert "Re-run failed stage" not in page.text


def test_resume_redirect_polls_past_the_old_terminal_manifest(
    examples_dir, client, monkeypatch,
):
    _write_one_stage_project(examples_dir)
    _write_manifest(examples_dir, "cancelled")
    monkeypatch.setattr("app.web.routers.runs.run_service.resume", lambda *_: None)

    response = client.post(
        f"/project/{PROJ}/runs/{RUN}/resume", follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/project/{PROJ}/runs/{RUN}?resuming=1"
    page = client.get(response.headers["location"])
    assert "const RESUMING = true;" in page.text
    assert "let lastSig = null, timer = null, sawRunning = RUNNING;" in page.text
    marker_clear = "url.searchParams.delete('resuming');"
    terminal_reload = "if (d.terminal && sawRunning)"
    assert marker_clear in page.text
    assert "`${url.pathname}${url.search}${url.hash}`" in page.text
    assert page.text.index(marker_clear) < page.text.index(terminal_reload)
    assert terminal_reload in page.text
    assert "setInterval" in page.text


def test_run_detail_page_hides_the_resume_cta_for_a_completed_run(examples_dir, client):
    """A clean run asks nothing of the reader, and has nothing for Restart to run."""
    _write_one_stage_project(examples_dir)
    _write_status_manifest(examples_dir, [("load", "ok")], "ok")

    page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert page.status_code == 200
    assert f'action="/project/{PROJ}/runs/{RUN}/resume"' not in page.text
    assert "Resume cancelled run" not in page.text
    assert "Re-run failed stage" not in page.text


@pytest.mark.parametrize("status", ["running", "errors", "cancelled", "awaiting_review"])
def test_run_menu_offers_restart_while_a_stage_is_left_to_run(examples_dir, client, status):
    _write_one_stage_project(examples_dir)
    _write_status_manifest(examples_dir, [("load", "ok"), ("score", "pending")], status)

    page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert page.status_code == 200
    assert "<h3>Restart run</h3>" in page.text
    restart_form = page.text.split('class="run-restart-form"')[0].rsplit("<form", 1)[-1]
    assert f'action="/project/{PROJ}/runs/{RUN}/resume"' in restart_form


def test_restart_goes_inert_once_every_stage_has_completed(examples_dir, client):
    """A resume would run none of them, and a control that changes nothing reads as
    a broken one."""
    _write_one_stage_project(examples_dir)
    _write_status_manifest(
        examples_dir, [("load", "ok"), ("score", "validation_warnings")], "ok")

    page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert page.status_code == 200
    assert f'action="/project/{PROJ}/runs/{RUN}/resume"' not in page.text
    assert "Every stage completed" in page.text
    assert f'href="/project/{PROJ}/runs/new?from_run={RUN}"' in page.text


def test_a_running_run_is_offered_restart_whatever_its_stages_say(examples_dir, client):
    """The zombie case: a dead executor leaves `running` behind, and Cancel needs one."""
    _write_one_stage_project(examples_dir)
    _write_status_manifest(examples_dir, [("load", "ok")], "running")

    page = client.get(f"/project/{PROJ}/runs/{RUN}")
    assert "last write wins" in page.text
    restart_form = page.text.split('class="run-restart-form"')[0].rsplit("<form", 1)[-1]
    assert f'action="/project/{PROJ}/runs/{RUN}/resume"' in restart_form


def test_restarting_drops_a_cancel_the_dead_executor_never_read(
    examples_dir, client, monkeypatch,
):
    """A dead executor leaves the cancel unread; the restarted run must not consume it."""
    _write_one_stage_project(examples_dir)
    _write_manifest(examples_dir, "running")
    client.post(f"/project/{PROJ}/runs/{RUN}/cancel", follow_redirects=False)
    monkeypatch.setattr("app.services.run.load_version_stages", lambda *a, **k: [])
    monkeypatch.setattr("app.services.run._run_in_background", lambda *a, **k: None)

    response = client.post(
        f"/project/{PROJ}/runs/{RUN}/resume", follow_redirects=False,
    )

    assert response.status_code == 303
    assert consume_cancel(PROJ, RUN) is False


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
