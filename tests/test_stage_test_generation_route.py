"""The TestClient is used as a context manager so its event loop survives across the
POST and the follow-up status polls; the faked turn runs on that same loop.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.compiler.stage_tests as compiler_stage_tests
from app.compiler.turn_failure import GENERATION_FAILURE_PREFIX
from app.core.agent.store import SessionStore
from app.core.agent.turns import TurnManager
from app.models import TableSchema
from app.models.stages.stage_tests import PythonRowFunctionStageTest
from app.compiler.stage_tests_submission import build_selector_submission_model
from app.main import app
from app.services import workspace
from app.services.methodology import write_methodology
from app.services.stage_test_rows import load_stage_row_sources
from run_seed import store_manifest
from stage_seed import add_stage, read_stage, read_stages, set_stages

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}


def _seed_project(root: Path) -> Path:
    project_dir = root / "alpha"
    project_dir.mkdir(parents=True, exist_ok=True)
    write_methodology((project_dir).name, "Double the amount.")
    pdir = project_dir
    pdir.mkdir(parents=True, exist_ok=True)
    add_stage(pdir, {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    })
    add_stage(pdir, {
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
        "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
    })
    add_stage(pdir, {
        "id": "report", "description": "Report", "type": "report",
        "inputs": [{"id": "double"}],
        "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [], "code": "def transform(df, output_dir):\n    return df\n"},
        "report": {}, "signature": {"form": "replaces"},
    })
    _write_run(project_dir)
    return project_dir


_RUN_ID = "20260101T000000"


def _write_run(project_dir: Path) -> None:
    """Examples are selected from a finished run, so the fixture project has one."""
    outputs = workspace.resolve_project_dir(
        project_dir.name) / "runs" / _RUN_ID / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"amount": [2.0, 7.5, 0.0]}).to_parquet(
        outputs / "load.parquet", index=False)
    store_manifest(project_dir, _RUN_ID, {
        "run_id": _RUN_ID, "started_at": _RUN_ID, "project": project_dir.name,
        "workflow_version": _RUN_ID, "human_review_queue_stats": {}, "status": "ok",
        "stage_records": [{
            "stage_id": "load", "type": "input_data", "status": "ok",
            "output_row_count": 3, "elapsed_ms": 1, "input_validation_report": [],
            "output_validation_report": None, "error": None,
            "output_path": "outputs/load.parquet",
        }],
    })


def _valid_suite(project: str = "alpha") -> Any:
    suite_model = build_selector_submission_model(
        PythonRowFunctionStageTest,
        {"load": TableSchema.model_validate(_IN_SCHEMA)},
        TableSchema.model_validate(_OUT_SCHEMA),
        load_stage_row_sources(project, {"load": TableSchema.model_validate(_IN_SCHEMA)}),
    )
    return suite_model.model_validate({
        "tests": [{
            "name": "doubles_two",
            "description": "the ordinary case",
            "selected_rows": [{"input": "load", "row": 0, "filter": "amount == 2.0"}],
            "expected": [{"amount": 2.0, "doubled": 4.0}],
        }]
    })


class _FakeGeneratorAgent:
    task = "generate tests for stage `double` and submit them"

    def __init__(self) -> None:
        self._answer: Any = None

    @property
    def answer(self) -> Any:
        return self._answer

    def build_engine(self) -> Any:
        agent = self

        class _Engine:
            async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
                emit({"kind": "text", "text": "generated"})
                agent._answer = _valid_suite()
                return [{"role": "assistant", "parts": [{"type": "text", "text": "generated"}]}], None

        return _Engine()


class _FakeGeneratorAgentNoAnswer:
    task = "generate tests for stage `double` and submit them"

    def __init__(self) -> None:
        self._answer: Any = None

    @property
    def answer(self) -> Any:
        return self._answer

    def build_engine(self) -> Any:
        class _Engine:
            async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
                emit({"kind": "text", "text": "gave up"})
                return [{"role": "assistant", "parts": [{"type": "text", "text": "gave up"}]}], None

        return _Engine()


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """TestClient must be a context manager: the background turn needs the loop to outlive the POST."""
    workspace.set_projects_dir(tmp_path)
    with TestClient(app) as c:
        yield c


def _poll_until_inactive(client: TestClient, project: str, sid: str, *,
                          timeout: float = 5.0, interval: float = 0.02) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/project/{project}/generation-session/{sid}/status")
        assert response.status_code == 200
        data = response.json()
        if not data["active"]:
            return data
        time.sleep(interval)
    pytest.fail("generation did not finish within the poll timeout")


# ── POST generate-tests ──────────────────────────────────────────────────────

def test_generate_tests_generates_and_patches_the_stage(client: TestClient, tmp_path: Path, monkeypatch):
    project_dir = _seed_project(tmp_path)
    monkeypatch.setattr(compiler_stage_tests, "default_turn_manager", lambda: TurnManager())
    monkeypatch.setattr(compiler_stage_tests, "build_stage_test_generator", lambda *a, **k: _FakeGeneratorAgent())

    response = client.post("/project/alpha/node/double/generate-tests")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    sid = data["session"]

    status = _poll_until_inactive(client, "alpha", sid)
    assert status["error"] is None

    stage = read_stage(project_dir, "double")
    assert stage["tests"][0]["name"] == "doubles_two"


def test_generate_tests_rejects_non_python_stage(client: TestClient, tmp_path: Path):
    _seed_project(tmp_path)
    before = len(SessionStore().list_sessions())

    response = client.post("/project/alpha/node/report/generate-tests")

    assert response.status_code == 400
    assert "can run them" in response.json()["detail"]
    assert len(SessionStore().list_sessions()) == before  # no orphaned session


def test_status_reports_error_after_failed_generation(client: TestClient, tmp_path: Path, monkeypatch):
    project_dir = _seed_project(tmp_path)
    monkeypatch.setattr(compiler_stage_tests, "default_turn_manager", lambda: TurnManager())
    monkeypatch.setattr(
        compiler_stage_tests, "build_stage_test_generator", lambda *a, **k: _FakeGeneratorAgentNoAnswer()
    )

    response = client.post("/project/alpha/node/double/generate-tests")
    sid = response.json()["session"]

    status = _poll_until_inactive(client, "alpha", sid)

    assert status["error"] is not None
    assert status["error"].startswith(GENERATION_FAILURE_PREFIX)
    stage = read_stage(project_dir, "double")
    assert "tests" not in stage  # nothing written on a failed generation


def test_generate_tests_rejects_python_stage_without_a_signature(client: TestClient, tmp_path: Path):
    project_dir = _seed_project(tmp_path)
    add_stage(project_dir, {
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
    })
    before = len(SessionStore().list_sessions())

    response = client.post("/project/alpha/node/double/generate-tests")

    assert response.status_code == 400
    assert "signature" in response.json()["detail"]
    assert len(SessionStore().list_sessions()) == before  # no orphaned session


def test_generate_tests_maps_workflow_load_error_to_400(client: TestClient, tmp_path: Path):
    _seed_project(tmp_path)
    stages = read_stages(tmp_path / "alpha")
    stages[0] = {"id": "load", "type": "not_a_real_type"}
    set_stages(tmp_path / "alpha", stages)

    response = client.post("/project/alpha/node/double/generate-tests")

    assert response.status_code == 400
    assert "not_a_real_type" in response.json()["detail"]


def test_status_unknown_session_is_404(client: TestClient, tmp_path: Path):
    _seed_project(tmp_path)

    response = client.get("/project/alpha/generation-session/doesnotexist/status")

    assert response.status_code == 404


# ── Template: the button appears only for python-transform stages ───────────

def test_review_partial_shows_generate_tests_button_for_python_stage(client: TestClient, tmp_path: Path):
    _seed_project(tmp_path)

    response = client.get("/project/alpha/node/double/panel")

    assert response.status_code == 200
    assert '<button type="button" class="btn" data-role="generate-tests"' in response.text
    assert "Generate tests" in response.text


@pytest.mark.parametrize("stage_id", ["report", "load"])
def test_review_partial_hides_generate_tests_button_for_non_python_stage(
    client: TestClient, tmp_path: Path, stage_id: str
):
    _seed_project(tmp_path)

    response = client.get(f"/project/alpha/node/{stage_id}/panel")

    assert response.status_code == 200
    assert '<button type="button" class="btn" data-role="generate-tests"' not in response.text
