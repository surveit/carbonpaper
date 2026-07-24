"""Offline tests for the compile mechanism (no LLM / no CLI).

`compile_methodology` is a thin wrapper over the Agent[Workflow] engine
(app.compiler.workflow.build_workflow_agent): these tests stub `build_workflow_agent`
so `.run()` resolves to a canned Workflow, or raises GenerationError to simulate the
real no-submission failure mode, without touching the LLM/CLI.
"""

from __future__ import annotations

import pytest

from app.compiler import compiler
from app.core.errors import CompilationError, GenerationError
from app.models.workflow import Workflow


def _stage(tmp_path):
    return {
        "id": "load", "type": "input_data", "name": "Load documents",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "data" / "docs.csv"), "format": "csv"}},
        "output_schema": {"columns": [{"name": "doc_id", "type": "str"}]},
    }


class _FakeAgent:
    """Stand-in for app.core.agent.agent.Agent: `.run()` resolves to a canned Workflow
    without an LLM/CLI."""

    def __init__(self, answer):
        self._answer = answer

    async def run(self):
        return self._answer


class _FailingFakeAgent:
    """Stand-in for app.core.agent.agent.Agent whose `.run()` raises GenerationError,
    as the real Agent does when no valid answer is submitted within its attempt budget."""

    async def run(self):
        raise GenerationError("agent submitted no valid Workflow in 3 attempt(s)")


def test_compile_methodology_returns_result_contract(monkeypatch, tmp_path):
    workflow = Workflow.model_validate({"stages": [_stage(tmp_path)]})
    monkeypatch.setattr(compiler, "build_workflow_agent", lambda *a, **k: _FakeAgent(workflow))

    result = compiler.compile_methodology("some prose", "demo")

    assert result["name"] == "demo"
    assert len(result["stages"]) == 1
    assert result["stages"][0]["id"] == "load"
    assert result["stages"][0]["type"] == "input_data"
    assert result["validation"] == []
    assert result["methodology_raw"] == ""
    assert result["prompt"] == ""
    assert result["raw_llm"] == ""


def test_compile_methodology_raises_when_generation_fails(monkeypatch):
    monkeypatch.setattr(compiler, "build_workflow_agent", lambda *a, **k: _FailingFakeAgent())

    with pytest.raises(CompilationError) as exc_info:
        compiler.compile_methodology("some prose", "demo")

    assert isinstance(exc_info.value.__cause__, GenerationError)


def test_validate_delegates_to_models_validate_workflow_draft(monkeypatch):
    calls = []
    monkeypatch.setattr(
        compiler.models, "validate_workflow_draft", lambda stages: calls.append(stages) or ["issue"]
    )
    assert compiler.validate([{"id": "s1"}]) == ["issue"]
    assert calls == [[{"id": "s1"}]]
