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
    """The suite model for the `double` stage: one input `load` carrying
    _IN_SCHEMA, a python_row_function so each test is one row in / one row out."""
    return build_stage_tests_model(
        PythonRowFunctionStageTest,
        {"load": TableSchema.model_validate(_IN_SCHEMA)},
        TableSchema.model_validate(output_schema),
    )


def _seed_project(project_dir: Path, *, existing_tests: list[dict] | None = None) -> None:
    """A project with a document and a two-stage workflow (load -> double), mirroring the
    fixture in tests/test_version_gate_stage_tests.py. `double` is the python_row_function
    stage tests are generated for."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "document.md").write_text("Double the amount.", encoding="utf-8")
    compiled = project_dir / "compiled"
    compiled.mkdir()
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _IN_SCHEMA,
    }), encoding="utf-8")
    double_spec: dict[str, Any] = {
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
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
    """`{"tests": []}` validates as a suite (there is no case to refuse), but writing it
    through would wipe any existing tests while reporting success. The completion hook
    must reject it before it reaches stage_edit."""
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
    """A null cell in an expected row (e.g. a nullable column the transform sometimes
    leaves unset) must survive the write path intact. `expected` is typed
    `list[dict[str, Any]]` — a plain dict, not a sub-model — so pydantic's
    `exclude_none=True` (used both when building the patch and when write_stage
    re-serializes the validated Stage) only drops None MODEL FIELDS; it does not walk
    into that dict to strip None entries. And patch_stage_spec's RFC 7386 merge patch
    replaces the whole `tests` array wholesale (a list value is returned as-is, never
    recursed into) rather than deep-merging its contents. Pins that neither step
    silently turns a declared null into a missing key."""
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
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _IN_SCHEMA,
    }), encoding="utf-8")
    (compiled / "02_double.json").write_text(json.dumps({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": out_schema,
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
    """Stands in for the stage-test generator Agent driven as a live turn: stream_turn
    'submits' a StageTestSuite and the engine returns a transcript, exactly as
    submit_answer + the real engine would during the turn."""

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
    """`load` is an input_data stage — tests cannot be generated for it. The type check must
    run before the session is created, so no orphaned session is left behind."""
    project_dir = tmp_path / "demo"
    _seed_project(project_dir)
    store = SessionStore()
    monkeypatch.setattr(compiler_stage_tests, "open_session_store", lambda: store)

    before = len(store.list_sessions())
    with pytest.raises(ValueError, match="can run them"):
        generation.start_stage_test_generation(project_dir, stage_id="load", model="sonnet")

    assert len(store.list_sessions()) == before  # no orphaned session


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
    assert session["context"]["phase"] == "stage_tests"
    assert session["context"]["stage_id"] == "double"
    assert session["agent_id"] is None  # view-only
    assert session["messages"]  # TurnManager persisted the conversation

    stage = json.loads((project_dir / "compiled" / "02_double.json").read_text(encoding="utf-8"))
    assert stage["tests"][0]["name"] == "doubles_two"  # completion hook patched the stage


class _FakeGeneratorAgentNoAnswer:
    """Stands in for a generator whose turn ends without ever calling submit_answer — the
    no-answer path _finish_stage_tests turns into a GenerationError, exercising the
    on_done failure-persistence wrapper in app.compiler.stage_tests."""

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
    """A generation turn that ends with no submitted suite raises inside the completion hook.
    That failure must not be lost to anyone who wasn't watching the live turn: it lands in
    the session's persisted transcript, and the stage file is left unpatched."""
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
