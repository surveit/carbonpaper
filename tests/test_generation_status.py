"""The shared background-job status mechanism (issue #95): the atomic JSON writer both
the runtime manifest and generation use, the generation status-file lifecycle, that a
FAILED generation is surfaced loudly (error persisted, not a silent no-schemas state),
and the /generation-status poll endpoint.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.web.config as web_config
import app.web.routers.project as project_router
from app.main import app
from app.services import generation, job_status

client = TestClient(app)


# ── Atomic writer ────────────────────────────────────────────────────────────

def test_atomic_write_json_writes_valid_json_and_leaves_no_temp(tmp_path: Path):
    target = tmp_path / "manifest.json"
    job_status.atomic_write_json(target, {"status": "running", "n": 1})
    assert json.loads(target.read_text()) == {"status": "running", "n": 1}
    # The temp sibling used for the rename must not survive a successful write.
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_json_overwrites_in_place(tmp_path: Path):
    target = tmp_path / "manifest.json"
    job_status.atomic_write_json(target, {"status": "running"})
    job_status.atomic_write_json(target, {"status": "ok"})
    assert json.loads(target.read_text())["status"] == "ok"


def test_atomic_write_json_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "nested" / "deep" / "manifest.json"
    job_status.atomic_write_json(target, {"ok": True})
    assert target.exists()


def test_atomic_write_json_never_observes_a_half_written_file(tmp_path: Path):
    """The reader either sees the WHOLE old file or the WHOLE new one. We simulate a
    read landing during the write by hooking os.replace: at the moment the new content
    is staged in the temp file (but not yet renamed), the live path must still parse as
    the complete previous document — never a truncated mix."""
    import app.services.job_status as js

    target = tmp_path / "manifest.json"
    js.atomic_write_json(target, {"status": "running", "stages": [1, 2, 3]})

    observed: list[dict] = []
    real_replace = js.os.replace

    def _replace(src, dst):
        # A concurrent poll reads the destination just before the rename commits.
        observed.append(json.loads(Path(dst).read_text()))
        return real_replace(src, dst)

    js.os.replace = _replace
    try:
        js.atomic_write_json(target, {"status": "ok", "stages": [1, 2, 3, 4, 5]})
    finally:
        js.os.replace = real_replace

    # What the "poll" saw pre-rename was the intact previous document, not a half write.
    assert observed == [{"status": "running", "stages": [1, 2, 3]}]
    assert json.loads(target.read_text())["status"] == "ok"


# ── Generation status lifecycle ──────────────────────────────────────────────

def test_generation_status_lifecycle(tmp_path: Path):
    status = job_status.init_generation_status(tmp_path, model="sonnet")
    assert status["status"] == "running"
    assert [p["status"] for p in status["phases"]] == ["pending", "pending"]

    job_status.phase_running(tmp_path, status, "data_model")
    job_status.phase_ok(tmp_path, status, "data_model")
    job_status.phase_running(tmp_path, status, "workflow")
    job_status.phase_ok(tmp_path, status, "workflow")
    job_status.generation_done(tmp_path, status)

    loaded = job_status.load_generation_status(tmp_path)
    assert loaded is not None
    assert loaded["status"] == "ok"
    assert job_status.generation_phase(loaded, "data_model")["status"] == "ok"
    assert job_status.generation_phase(loaded, "workflow")["status"] == "ok"


def test_load_generation_status_is_none_when_absent(tmp_path: Path):
    assert job_status.load_generation_status(tmp_path) is None


def test_phase_failed_records_the_error_loudly(tmp_path: Path):
    status = job_status.init_generation_status(tmp_path, model="sonnet")
    job_status.phase_running(tmp_path, status, "data_model")
    job_status.phase_failed(
        tmp_path, status, "data_model", ValueError("agent returned no schemas")
    )

    loaded = job_status.load_generation_status(tmp_path)
    assert loaded["status"] == "error"                       # whole job marked failed
    dm = job_status.generation_phase(loaded, "data_model")
    assert dm["status"] == "error"
    assert dm["error"]["type"] == "ValueError"
    assert "no schemas" in dm["error"]["message"]            # the reason is persisted
    # Top-level convenience error names the phase, for the page banner.
    assert loaded["error"]["phase"] == "data_model"
    assert "no schemas" in loaded["error"]["message"]


def test_phase_failed_accepts_a_plain_string_reason(tmp_path: Path):
    """Workflow validation issues are not raised as exceptions; the reason is a string."""
    status = job_status.init_generation_status(tmp_path, model="sonnet")
    job_status.phase_failed(tmp_path, status, "workflow", "compiled workflow failed validation: x")
    loaded = job_status.load_generation_status(tmp_path)
    assert loaded["error"]["message"].startswith("compiled workflow failed validation")


# ── The orchestrator surfaces failures ───────────────────────────────────────

def test_failed_data_model_generation_is_surfaced(tmp_path: Path, monkeypatch):
    """A data-model phase that raises leaves NO schemas — the old behaviour was
    indistinguishable from "not generated yet". Now the failure is recorded on the
    status file with its message, so the page can render it."""
    async def _boom(document, *, model):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(generation, "compile_data_model", _boom)
    status = job_status.init_generation_status(tmp_path, model="sonnet")
    ok = generation._generate_data_model(tmp_path, "some document", "sonnet", status)

    assert ok is False
    loaded = job_status.load_generation_status(tmp_path)
    assert loaded["status"] == "error"
    assert loaded["error"]["message"] == "backend unavailable"
    # No schemas were written (the failure did not fake a success).
    assert not (tmp_path / "schemas").exists()


def test_failed_workflow_validation_is_surfaced(tmp_path: Path, monkeypatch):
    """A workflow compile that returns validation issues is surfaced loudly (issues in
    the message) and not written as a broken workflow."""
    monkeypatch.setattr(
        generation, "compile_methodology",
        lambda document, name, *, model: {"validation": ["01_x.json: bad"], "stages": []},
    )
    status = job_status.init_generation_status(tmp_path, model="sonnet")
    ok = generation._generate_workflow(tmp_path, tmp_path.name, "doc", "sonnet", status)

    assert ok is False
    loaded = job_status.load_generation_status(tmp_path)
    wf = job_status.generation_phase(loaded, "workflow")
    assert wf["status"] == "error"
    assert "01_x.json: bad" in wf["error"]["message"]


def test_run_generation_stops_after_a_failed_data_model(tmp_path: Path, monkeypatch):
    """The chain stops at a failed data model — the workflow is never built on it."""
    async def _boom(document, *, model):
        raise RuntimeError("nope")

    monkeypatch.setattr(generation, "compile_data_model", _boom)
    called = {"workflow": False}
    monkeypatch.setattr(
        generation, "compile_methodology",
        lambda *a, **k: called.__setitem__("workflow", True) or {"validation": [], "stages": []},
    )
    status = job_status.init_generation_status(tmp_path, model="sonnet")
    generation._run_generation(tmp_path, tmp_path.name, "doc", "sonnet", status)

    assert called["workflow"] is False                       # workflow phase never ran
    loaded = job_status.load_generation_status(tmp_path)
    assert loaded["status"] == "error"
    assert job_status.generation_phase(loaded, "workflow")["status"] == "pending"


# ── The poll endpoint ────────────────────────────────────────────────────────

@pytest.fixture()
def project_dir(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    monkeypatch.setattr(web_config, "EXAMPLES_DIR", tmp_path, raising=False)
    monkeypatch.setattr(project_router, "EXAMPLES_DIR", tmp_path, raising=False)
    return tmp_path / "proj"


def test_generation_status_endpoint_idle_when_never_generated(project_dir):
    r = client.get("/project/proj/generation-status")
    assert r.status_code == 200
    assert r.json() == {"status": "idle", "terminal": True, "phases": {}, "error": None}


def test_generation_status_endpoint_reports_running_phases(project_dir):
    status = job_status.init_generation_status(project_dir, model="sonnet")
    job_status.phase_running(project_dir, status, "data_model")

    r = client.get("/project/proj/generation-status")
    body = r.json()
    assert body["status"] == "running"
    assert body["terminal"] is False
    assert body["phases"] == {"data_model": "running", "workflow": "pending"}


def test_generation_status_endpoint_reports_failure(project_dir):
    status = job_status.init_generation_status(project_dir, model="sonnet")
    job_status.phase_failed(project_dir, status, "data_model", ValueError("kaboom"))

    body = client.get("/project/proj/generation-status").json()
    assert body["status"] == "error"
    assert body["terminal"] is True
    assert body["error"]["message"] == "kaboom"


def test_data_model_page_surfaces_a_failed_generation(project_dir):
    """End-to-end: the data-model page renders the loud failure banner (not the silent
    'no data model yet' 0-state) when the data-model generation phase errored."""
    status = job_status.init_generation_status(project_dir, model="sonnet")
    job_status.phase_failed(project_dir, status, "data_model", ValueError("agent crashed"))

    r = client.get("/project/proj/data_model")
    assert r.status_code == 200
    assert "Data-model generation failed" in r.text
    assert "agent crashed" in r.text
    assert "No data model yet" not in r.text                 # not the silent 0-state
