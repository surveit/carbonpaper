"""Generation-time stage-test pipeline (app.web.stage_test_derivation): after a workflow is
generated, each python transform's tests are derived headlessly, run against the stage's real
code, and the code is repaired until green — or generation fails loudly. Human-touched tests are
frozen across a regenerate (app.services.generation._finish_workflow).

The deriver and repair agents are faked (no CLI, no LLM); run_stage_tests executes the REAL
stage code, so green/red is genuine. Driven with asyncio.run, mirroring the sibling suites.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import app.services.generation as generation
import app.web.stage_test_derivation as pipeline
from app.core.errors import GenerationError
from app.core.models.stages.stage_tests import (
    GENERATED_ORIGIN,
    build_stage_tests_model,
    stage_tests_are_frozen,
)
from app.core.models.workflow import Workflow
from app.services.loader import load_workflow
from app.services.stage_tests import RepairedStageCode

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}
_CORRECT = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"
_BUGGY = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 3}\n"


def _seed(project_dir: Path, *, code: str, tests: list[dict] | None = None) -> None:
    """A load -> double workflow. `double` is the python_row_function under test; `code` is its
    function body and `tests` its pre-existing StageTest cases (None = no tests yet)."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "document.md").write_text("Double the amount.", encoding="utf-8")
    compiled = project_dir / "compiled"
    compiled.mkdir(exist_ok=True)
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA,
    }), encoding="utf-8")
    double: dict[str, Any] = {
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}], "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline", "code": code},
    }
    if tests is not None:
        double["tests"] = tests
    (compiled / "02_double.json").write_text(json.dumps(double), encoding="utf-8")


def _suite(cases: list[dict]) -> Any:
    """A validated StageTestSuite for `double`'s shape (one input `load`, 1 row in/out)."""
    return build_stage_tests_model("python_row_function", ["load"]).model_validate({"tests": cases})


_DOUBLES_TWO = {
    "name": "doubles_two", "inputs": {"load": [{"amount": 2.0}]},
    "expected": [{"amount": 2.0, "doubled": 4.0}],
}


class _FakeAgent:
    """A stubbed headless Agent: run() returns a preset answer without any CLI/LLM."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer

    async def run(self) -> Any:
        return self._answer


def _fake_deriver(monkeypatch: Any, suite: Any) -> None:
    monkeypatch.setattr(
        pipeline, "build_stage_test_deriver",
        lambda document, stage, *, model: _FakeAgent(suite),
    )


def _fake_repairer(monkeypatch: Any, codes: list[str]) -> list[str]:
    """Repair agent returns `codes` in order across successive attempts. Returns the list of
    failure reports it was handed, so a test can assert the repairer saw the diffs."""
    seen: list[str] = []
    it = iter(codes)

    def build(stage: Any, failure_report: str, *, model: str) -> _FakeAgent:
        seen.append(failure_report)
        return _FakeAgent(RepairedStageCode(code=next(it)))

    monkeypatch.setattr(pipeline, "build_stage_test_repair_agent", build)
    return seen


def _double(project_dir: Path) -> Any:
    return next(s for s in load_workflow(project_dir) if s.id == "double")


# ── 1. successful generation: tests derived + stages green ────────────────────────────

def test_generation_writes_generated_tests_and_stage_is_green(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    _seed(project_dir, code=_CORRECT)  # correct code — the derived suite passes as-is
    _fake_deriver(monkeypatch, _suite([_DOUBLES_TWO]))

    asyncio.run(pipeline.generate_stage_tests_for_workflow(project_dir, document="doc", model="sonnet"))

    stage = _double(project_dir)
    assert [t.name for t in stage.tests] == ["doubles_two"]
    assert all(t.origin == GENERATED_ORIGIN for t in stage.tests)  # stamped machine-authored
    assert not stage_tests_are_frozen(stage.tests)                 # so a later regen may re-derive


# ── 2. a red stage triggers the repair loop and succeeds within budget ────────────────

def test_red_stage_is_repaired_within_budget(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    _seed(project_dir, code=_BUGGY)  # *3, so the derived suite (expects *2) is red
    _fake_deriver(monkeypatch, _suite([_DOUBLES_TWO]))
    # First repair attempt still wrong, second fixes it — succeeds on attempt 2 of 3.
    seen = _fake_repairer(monkeypatch, [_BUGGY, _CORRECT])

    asyncio.run(pipeline.generate_stage_tests_for_workflow(project_dir, document="doc", model="sonnet"))

    stage = _double(project_dir)
    assert stage.function.code == _CORRECT                      # code repaired, tests kept
    from app.runtime.stage_tests import run_stage_tests
    assert all(r.status == "passed" for r in run_stage_tests(stage))
    assert "doubled" in seen[0] and "expected 4.0" in seen[0]   # repairer saw the failing diff


# ── 3. a red stage that never turns green fails loudly ────────────────────────────────

def test_unrepairable_red_stage_fails_loudly(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    _seed(project_dir, code=_BUGGY)
    _fake_deriver(monkeypatch, _suite([_DOUBLES_TWO]))
    _fake_repairer(monkeypatch, [_BUGGY, _BUGGY, _BUGGY])  # never fixes it, exhausts the 3 attempts

    with pytest.raises(GenerationError, match="still fails its tests after 3 repair"):
        asyncio.run(
            pipeline.generate_stage_tests_for_workflow(project_dir, document="doc", model="sonnet")
        )


def test_repair_budget_is_exactly_three_attempts(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    _seed(project_dir, code=_BUGGY)
    _fake_deriver(monkeypatch, _suite([_DOUBLES_TWO]))
    seen = _fake_repairer(monkeypatch, [_BUGGY, _BUGGY, _BUGGY])

    with pytest.raises(GenerationError):
        asyncio.run(
            pipeline.generate_stage_tests_for_workflow(project_dir, document="doc", model="sonnet")
        )
    assert len(seen) == pipeline.MAX_REPAIR_ATTEMPTS == 3  # no more, no fewer


# ── 4. human-touched tests are frozen across regeneration ─────────────────────────────

_HUMAN_CASE = {
    "name": "human_authored", "inputs": {"load": [{"amount": 5.0}]},
    "expected": [{"amount": 5.0, "doubled": 10.0}],
    # no `origin` — a hand-authored case
}


def test_frozen_tests_skipped_by_the_pipeline(tmp_path: Path, monkeypatch: Any):
    """A stage already carrying a human case must be left alone: the deriver is never even built
    for it, and its test survives untouched."""
    project_dir = tmp_path / "demo"
    _seed(project_dir, code=_CORRECT, tests=[_HUMAN_CASE])

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deriver must not run for a stage with frozen tests")

    monkeypatch.setattr(pipeline, "build_stage_test_deriver", _boom)

    asyncio.run(pipeline.generate_stage_tests_for_workflow(project_dir, document="doc", model="sonnet"))

    stage = _double(project_dir)
    assert [t.name for t in stage.tests] == ["human_authored"]  # preserved verbatim
    assert stage_tests_are_frozen(stage.tests)


def test_regeneration_preserves_human_tests(tmp_path: Path):
    """_finish_workflow carries a frozen suite across the compiled/ wipe a regenerate performs."""
    project_dir = tmp_path / "demo"
    _seed(project_dir, code=_CORRECT, tests=[_HUMAN_CASE])

    # Regenerate the SAME two-stage workflow (a fresh Workflow the agent would submit, carrying
    # no tests) — _finish_workflow should snapshot the human case and restore it onto `double`.
    new_workflow = Workflow.model_validate({"stages": [
        {"id": "load", "name": "Load", "type": "input_data",
         "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA},
        {"id": "double", "name": "Double", "type": "python_row_function",
         "inputs": [{"id": "load", "schema": _IN_SCHEMA}], "output_schema": _OUT_SCHEMA,
         "function": {"kind": "inline", "code": _CORRECT}},
    ]})

    generation._finish_workflow(project_dir, "demo", new_workflow)

    stage = _double(project_dir)
    assert [t.name for t in stage.tests] == ["human_authored"]  # frozen suite survived the wipe
    assert stage_tests_are_frozen(stage.tests)


def test_regeneration_drops_machine_generated_tests(tmp_path: Path):
    """A purely machine-authored suite (all origin=generated) is NOT frozen: a regenerate lets it
    go, to be re-derived fresh against the new code rather than carried forward stale."""
    project_dir = tmp_path / "demo"
    generated_case = {**_DOUBLES_TWO, "origin": GENERATED_ORIGIN}
    _seed(project_dir, code=_CORRECT, tests=[generated_case])

    new_workflow = Workflow.model_validate({"stages": [
        {"id": "load", "name": "Load", "type": "input_data",
         "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA},
        {"id": "double", "name": "Double", "type": "python_row_function",
         "inputs": [{"id": "load", "schema": _IN_SCHEMA}], "output_schema": _OUT_SCHEMA,
         "function": {"kind": "inline", "code": _CORRECT}},
    ]})

    generation._finish_workflow(project_dir, "demo", new_workflow)

    stage = _double(project_dir)
    assert stage.tests is None  # not carried forward — the wipe cleared it for re-derivation
