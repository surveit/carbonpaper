"""The TestClient is used as a context manager so its event loop survives across the
POST and the follow-up status polls; the faked turn runs on that same loop.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.compiler.stage_tests as compiler_stage_tests
from app.compiler.turn_failure import GENERATION_FAILURE_PREFIX
from app.core.agent.store import SessionStore
from app.core.agent.turns import TurnManager
from app.models import TableSchema
from app.models.stages.stage_tests import (
    PythonRowFunctionStageTest,
    build_stage_tests_model,
)
from app.main import app
from app.services import workspace
from stage_seed import add_stage, read_stage, set_stages
from app.services.methodology import write_methodology

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}


def _seed_project(root: Path) -> Path:
    """A project (alpha) with a document and a three-stage workflow
    load -> double -> publish. `double` (python_row_function) is the stage tests
    are generated for; `publish` and `load` are non-python controls for the
    button/template assertions."""
    project_dir = root / "alpha"
    project_dir.mkdir(parents=True, exist_ok=True)
    write_methodology(project_dir.name, "Double the amount.")
    pdir = project_dir
    pdir.mkdir(parents=True, exist_ok=True)
    add_stage(pdir, {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    })
    add_stage(pdir, {
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
        "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
    })
    add_stage(pdir, {
        "id": "publish", "description": "Publish", "type": "publish",
        "inputs": [{"id": "double", "schema": _OUT_SCHEMA}],
        "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [], "code": "def transform(df, output_dir):\n    return df\n"},
        "publish": {}, "signature": {"form": "replaces"},
    })
    return project_dir


def _valid_suite() -> Any:
    """A validated StageTestSuite for the `double` stage's shape (one input
    `load`, a python_row_function so each test is one row in / one row out)."""
    suite_model = build_stage_tests_model(
        PythonRowFunctionStageTest,
        {"load": TableSchema.model_validate(_IN_SCHEMA)},
        TableSchema.model_validate(_OUT_SCHEMA),
    )
    return suite_model.model_validate({
        "tests": [{
            "name": "doubles_two",
            "inputs": {"load": [{"amount": 2.0}]},
            "expected": [{"amount": 2.0, "doubled": 4.0}],
        }]
    })


class _FakeGeneratorAgent:
    """Stands in for the stage-test generator Agent: stream_turn 'submits' a valid
    suite and returns a transcript, exactly as the real submit_answer + engine
    would during the turn."""

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
    """A generator whose turn ends without ever calling submit_answer — exercises
    the no-answer -> GenerationError -> persisted-failure path."""

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
    """The review-partial's TestClient fixture (tests/test_stage_test_panel.py),
    but held open as a context manager: the generation turn is fire-and-forget
    background work on the client's own event loop, so that loop must survive
    across the POST and the follow-up status polls, not be torn down after each
    request (starlette's TestClient tears down a fresh portal per call unless
    it's used as `with TestClient(app) as client:`)."""
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

    response = client.post("/project/alpha/node/publish/generate-tests")

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
    """A stage with no signature does not parse, so the route 400s while loading."""
    project_dir = _seed_project(tmp_path)
    add_stage(project_dir, {
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
    })
    before = len(SessionStore().list_sessions())

    response = client.post("/project/alpha/node/double/generate-tests")

    assert response.status_code == 400
    assert "signature" in response.json()["detail"]
    assert len(SessionStore().list_sessions()) == before  # no orphaned session


def test_generate_tests_maps_workflow_load_error_to_400(client: TestClient, tmp_path: Path):
    """A project whose workflow fails to load (here: one stage holds
    invalid JSON) makes `generation.start_stage_test_generation`'s `load_workflow`
    call raise `WorkflowLoadError` — the route must map that to 400, the same as the
    ValueError cases above, not let it propagate as an uncaught 500."""
    _seed_project(tmp_path)
    set_stages(tmp_path / "alpha", [{"id": "load", "type": "not_a_real_type"}])

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


@pytest.mark.parametrize("stage_id", ["publish", "load"])
def test_review_partial_hides_generate_tests_button_for_non_python_stage(
    client: TestClient, tmp_path: Path, stage_id: str
):
    _seed_project(tmp_path)

    response = client.get(f"/project/alpha/node/{stage_id}/panel")

    assert response.status_code == 200
    assert '<button type="button" class="btn" data-role="generate-tests"' not in response.text
