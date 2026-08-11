from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import app.compiler.stage_tests as compiler_stage_tests
import app.services.generation as generation
from app.compiler.turn_failure import GENERATION_FAILURE_PREFIX
from app.core.agent.store import SessionStore
from app.core.agent.turns import TurnManager
from app.core.errors import GenerationError
from app.models import TableSchema
from app.models.authoring_lifecycle_note import CompilerPhase
from app.models.stages.code import SUMMARY_MAX_CHARS
from app.models.stages.stage_tests import (
    PythonRowFunctionStageTest,
    build_stage_tests_model,
)

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}


def _suite_model(output_schema: dict = _OUT_SCHEMA) -> Any:
    return build_stage_tests_model(
        PythonRowFunctionStageTest,
        {"load": TableSchema.model_validate(_IN_SCHEMA)},
        TableSchema.model_validate(output_schema),
    )


def _seed_project(project_dir: Path, *, existing_tests: list[dict] | None = None) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "document.md").write_text("Double the amount.", encoding="utf-8")
    compiled = project_dir / "compiled"
    compiled.mkdir()
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    }), encoding="utf-8")
    double_spec: dict[str, Any] = {
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
        "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
    }
    if existing_tests is not None:
        double_spec["tests"] = existing_tests
    (compiled / "02_double.json").write_text(json.dumps(double_spec), encoding="utf-8")


def _valid_suite() -> Any:
    return _suite_model().model_validate({
        "tests": [{
            "name": "doubles_two",
            "inputs": {"load": [{"amount": 2.0}]},
            "expected": [{"amount": 2.0, "doubled": 4.0}],
        }]
    })


# ── _finish_stage_tests: the completion hook patches through stage_edit ──────────────────

def test_finish_stage_tests_patches_the_stage(tmp_path: Path):
    project_dir = tmp_path / "demo"
    _seed_project(project_dir)

    generation._finish_stage_tests(project_dir, "double", _valid_suite())

    stage = json.loads((project_dir / "compiled" / "02_double.json").read_text(encoding="utf-8"))
    assert len(stage["tests"]) == 1
    assert stage["tests"][0]["name"] == "doubles_two"


def test_finish_stage_tests_replaces_existing_tests(tmp_path: Path):
    project_dir = tmp_path / "demo"
    _seed_project(project_dir, existing_tests=[{
        "name": "old_case",
        "inputs": {"load": [{"amount": 1.0}]},
        "expected": [{"amount": 1.0, "doubled": 2.0}],
    }])

    generation._finish_stage_tests(project_dir, "double", _valid_suite())

    stage = json.loads((project_dir / "compiled" / "02_double.json").read_text(encoding="utf-8"))
    names = [t["name"] for t in stage["tests"]]
    assert names == ["doubles_two"]  # the old case is gone, wholesale replace


def test_finish_with_no_answer_raises(tmp_path: Path):
    project_dir = tmp_path / "demo"
    _seed_project(project_dir)

    with pytest.raises(GenerationError):
        generation._finish_stage_tests(project_dir, "double", None)

    stage = json.loads((project_dir / "compiled" / "02_double.json").read_text(encoding="utf-8"))
    assert "tests" not in stage  # nothing written on a failed generation


def test_finish_with_empty_suite_raises(tmp_path: Path):
    project_dir = tmp_path / "demo"
    _seed_project(project_dir, existing_tests=[{
        "name": "old_case",
        "inputs": {"load": [{"amount": 1.0}]},
        "expected": [{"amount": 1.0, "doubled": 2.0}],
    }])
    empty_suite = _suite_model().model_validate({"tests": []})

    with pytest.raises(GenerationError, match="empty test suite"):
        generation._finish_stage_tests(project_dir, "double", empty_suite)

    stage = json.loads((project_dir / "compiled" / "02_double.json").read_text(encoding="utf-8"))
    names = [t["name"] for t in stage["tests"]]
    assert names == ["old_case"]  # existing tests survive — nothing written on rejection


def test_finish_stage_tests_preserves_null_cells(tmp_path: Path):
    """`exclude_none=True` drops None MODEL FIELDS; `expected` is a plain dict it never walks."""
    project_dir = tmp_path / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "document.md").write_text("Double the amount.", encoding="utf-8")
    compiled = project_dir / "compiled"
    compiled.mkdir()
    out_schema = {"columns": [
        {"name": "amount", "type": "float", "nullable": False},
        {"name": "flag", "type": "bool", "nullable": True},
    ]}
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    }), encoding="utf-8")
    (compiled / "02_double.json").write_text(json.dumps({
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "signature": {"form": "extends", "adds": [
            c for c in out_schema["columns"] if c not in _IN_SCHEMA["columns"]]},
        "function": {"kind": "inline", "summary": "Test fixture step.", "corner_cases": [],
                     "code": "def transform(row):\n    return {**row, 'flag': None}\n"},
    }), encoding="utf-8")

    suite = _suite_model(out_schema).model_validate({
        "tests": [{
            "name": "flag_defaults_null",
            "inputs": {"load": [{"amount": 1.0}]},
            "expected": [{"amount": 1.0, "flag": None}],
        }]
    })

    generation._finish_stage_tests(project_dir, "double", suite)

    stage = json.loads((project_dir / "compiled" / "02_double.json").read_text(encoding="utf-8"))
    assert stage["tests"][0]["expected"][0] == {"amount": 1.0, "flag": None}


# ── start_stage_test_generation wiring: hidden view-only session + a live turn ───────────

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
                agent._answer = _valid_suite()  # the submit_answer tool would set this
                return [{"role": "assistant", "parts": [{"type": "text", "text": "generated"}]}], None

        return _Engine()


def test_start_raises_before_session_for_non_python_stage(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    _seed_project(project_dir)
    store = SessionStore()
    monkeypatch.setattr(compiler_stage_tests, "open_session_store", lambda: store)

    before = len(store.list_sessions())
    with pytest.raises(ValueError, match="can run them"):
        generation.start_stage_test_generation(project_dir, stage_id="load", model="sonnet")

    assert len(store.list_sessions()) == before  # no orphaned session


def test_start_refuses_a_summary_the_write_path_would_reject(tmp_path: Path, monkeypatch: Any):
    """Refused BEFORE the turn, not after its cost is spent."""
    project_dir = tmp_path / "demo"
    _seed_project(project_dir)
    spec_path = project_dir / "compiled" / "02_double.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["function"]["summary"] = "x" * (SUMMARY_MAX_CHARS + 1)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    store = SessionStore()
    monkeypatch.setattr(compiler_stage_tests, "open_session_store", lambda: store)

    before = len(store.list_sessions())
    with pytest.raises(ValueError, match="cannot be written back"):
        generation.start_stage_test_generation(project_dir, stage_id="double", model="sonnet")

    assert len(store.list_sessions()) == before  # no session, so no turn was paid for


def test_start_creates_hidden_viewonly_session(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    _seed_project(project_dir)
    store = SessionStore()
    turns = TurnManager()
    # The app.core.agent bridge (session + live turn) lives in app.compiler.stage_tests, which
    # generation delegates to.
    monkeypatch.setattr(compiler_stage_tests, "open_session_store", lambda: store)
    monkeypatch.setattr(compiler_stage_tests, "default_turn_manager", lambda: turns)
    monkeypatch.setattr(compiler_stage_tests, "build_stage_test_generator", lambda *a, **k: _FakeGeneratorAgent())

    async def _drive() -> str:
        sid = generation.start_stage_test_generation(project_dir, stage_id="double", model="sonnet")
        assert store.load(sid)["pending_user"] == _FakeGeneratorAgent.task
        turn_id = store.load(sid)["active_turn"]
        assert turn_id, "a live turn should be active on the session while it generates"
        await turns._tasks[turn_id]
        return sid

    sid = asyncio.run(_drive())

    session = store.load(sid)
    assert session["context"]["hidden"] is True
    assert session["context"]["phase"] == CompilerPhase.BUILD
    assert session["context"]["stage_id"] == "double"
    assert session["agent_id"] is None  # view-only
    assert session["messages"]  # TurnManager persisted the conversation

    stage = json.loads((project_dir / "compiled" / "02_double.json").read_text(encoding="utf-8"))
    assert stage["tests"][0]["name"] == "doubles_two"  # completion hook patched the stage


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
                # No submit_answer call: agent.answer stays None.
                return [{"role": "assistant", "parts": [{"type": "text", "text": "gave up"}]}], None

        return _Engine()


def test_failed_generation_is_persisted_into_the_session(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    _seed_project(project_dir)
    store = SessionStore()
    turns = TurnManager()
    monkeypatch.setattr(compiler_stage_tests, "open_session_store", lambda: store)
    monkeypatch.setattr(compiler_stage_tests, "default_turn_manager", lambda: turns)
    monkeypatch.setattr(
        compiler_stage_tests, "build_stage_test_generator", lambda *a, **k: _FakeGeneratorAgentNoAnswer()
    )

    async def _drive() -> str:
        sid = generation.start_stage_test_generation(project_dir, stage_id="double", model="sonnet")
        turn_id = store.load(sid)["active_turn"]
        await turns._tasks[turn_id]
        return sid

    sid = asyncio.run(_drive())

    session = store.load(sid)
    failure_texts = [
        part.get("text", "")
        for message in session["messages"] if message.get("role") == "assistant"
        for part in message.get("parts", [])
        if part.get("type") == "text"
    ]
    assert any(GENERATION_FAILURE_PREFIX in text for text in failure_texts)

    stage = json.loads((project_dir / "compiled" / "02_double.json").read_text(encoding="utf-8"))
    assert "tests" not in stage  # nothing written on a failed generation
