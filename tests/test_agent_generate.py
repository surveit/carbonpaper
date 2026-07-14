"""The headless Agent (app.core.agent.agent.Agent): it produces a validated Pydantic object
by having the model CALL a submit_answer tool whose input schema IS the target model.

The submit/capture logic is tested directly (no CLI subprocess); run()'s loop is driven
over a FAKE engine whose stream_turn simulates the agent calling submit_answer with
scripted arguments. Coroutines are run with asyncio.run, mirroring tests/test_sdk_engine.py.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest
from pydantic import BaseModel

from app.core.agent.agent import Agent
from app.core.errors import GenerationError


class _Point(BaseModel):
    x: int
    y: int


class _FakeEngine:
    """Stands in for the SDK engine: stream_turn simulates the agent calling
    submit_answer with each scripted arg-dict. A rejected call raises (as the real tool
    would), which we swallow — the real agent would read the error and retry, which the
    next scripted call represents."""

    def __init__(self, submit: Callable[..., str], scripted_calls: list[dict[str, Any]]) -> None:
        self._submit = submit
        self._scripted = scripted_calls

    async def stream_turn(
        self, task: str, *, message_history: Any, emit: Any, resume: Any
    ) -> tuple[list[Any], None]:
        for fields in self._scripted:
            try:
                self._submit(**fields)
            except ValueError:
                pass  # rejected submission; the agent would fix + retry (next call)
        return [], None


def _agent(*, max_attempts: int = 4) -> "Agent[_Point]":
    return Agent(system_prompt="sp", target_schema=_Point, task="make a point", max_attempts=max_attempts)


def test_submit_answer_captures_the_validated_instance() -> None:
    agent = _agent()
    message = agent.submit_answer(x=1, y=2)
    assert agent._answer == _Point(x=1, y=2)  # captured, typed
    assert "Accepted" in message
    assert agent._attempts == 1


def test_submit_answer_rejects_invalid_with_pydantic_issues() -> None:
    agent = _agent()
    with pytest.raises(ValueError) as exc_info:
        agent.submit_answer(x=1)  # missing y
    assert "y" in str(exc_info.value)  # the field error is reported back
    assert agent._answer is None       # nothing captured on a bad submission
    assert agent._attempts == 1


def test_run_returns_the_submitted_answer_after_a_retry(monkeypatch: Any) -> None:
    agent = _agent()
    # First call is rejected (missing y), second is accepted — the loop returns it.
    fake = _FakeEngine(agent.submit_answer, [{"x": 1}, {"x": 1, "y": 2}])
    monkeypatch.setattr(agent, "build_engine", lambda: fake)
    result = asyncio.run(agent.run())
    assert result == _Point(x=1, y=2)


def test_run_raises_when_no_valid_answer_is_submitted(monkeypatch: Any) -> None:
    agent = _agent()
    fake = _FakeEngine(agent.submit_answer, [{"x": 1}, {"x": 2}])  # never valid
    monkeypatch.setattr(agent, "build_engine", lambda: fake)
    with pytest.raises(GenerationError) as exc_info:
        asyncio.run(agent.run())
    assert "_Point" in str(exc_info.value)  # names the target model


def test_answer_exposes_the_captured_submission_for_live_driving(monkeypatch: Any) -> None:
    # When driven as a live turn, the caller reads `answer` after the turn (rather than
    # run()'s return value) to persist the result.
    agent = _agent()
    assert agent.answer is None
    agent.submit_answer(x=3, y=4)
    assert agent.answer == _Point(x=3, y=4)
