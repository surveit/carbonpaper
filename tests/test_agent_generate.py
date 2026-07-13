"""The headless Agent (app.agent.agent.Agent): it produces a validated Pydantic object
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

from app.agent.agent import Agent
from app.errors import GenerationError


class _Point(BaseModel):
    x: int
    y: int


class _FakeEngine:
    """Stands in for the SDK engine: stream_turn simulates the agent calling
    submit_answer with each scripted arg-dict. A rejected call raises (as the real tool
    would), which we swallow — the real agent would read the error and retry, which the
    next scripted call represents."""

    def __init__(
        self,
        submit: Callable[..., str],
        scripted_calls: list[dict[str, Any]],
        *,
        transcript: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
    ) -> None:
        self._submit = submit
        self._scripted = scripted_calls
        self._transcript = transcript if transcript is not None else []
        self._session_id = session_id

    async def stream_turn(
        self, task: str, *, message_history: Any, emit: Any, resume: Any
    ) -> tuple[list[dict[str, Any]], str | None]:
        for fields in self._scripted:
            try:
                self._submit(**fields)
            except ValueError:
                pass  # rejected submission; the agent would fix + retry (next call)
        return self._transcript, self._session_id


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
    monkeypatch.setattr(agent, "_build_engine", lambda: fake)
    result = asyncio.run(agent.run())
    assert result == _Point(x=1, y=2)


def test_run_raises_when_no_valid_answer_is_submitted(monkeypatch: Any) -> None:
    agent = _agent()
    fake = _FakeEngine(agent.submit_answer, [{"x": 1}, {"x": 2}])  # never valid
    monkeypatch.setattr(agent, "_build_engine", lambda: fake)
    with pytest.raises(GenerationError) as exc_info:
        asyncio.run(agent.run())
    assert "_Point" in str(exc_info.value)  # names the target model


def test_run_captures_the_transcript_and_session_id(monkeypatch: Any) -> None:
    # The conversation the engine streamed is captured on the agent (not discarded),
    # so a caller can persist it as a viewable chat session after the run.
    agent = _agent()
    transcript = [
        {"role": "user", "parts": [{"type": "text", "text": "make a point"}]},
        {"role": "assistant", "parts": [{"type": "tool_call", "name": "submit_answer", "args": "{}"}]},
    ]
    fake = _FakeEngine(
        agent.submit_answer, [{"x": 1, "y": 2}], transcript=transcript, session_id="sess-1"
    )
    monkeypatch.setattr(agent, "_build_engine", lambda: fake)
    asyncio.run(agent.run())
    assert agent.transcript == transcript
    assert agent.session_id == "sess-1"


def test_transcript_is_captured_even_when_generation_fails(monkeypatch: Any) -> None:
    # A run that never submits a valid answer still captures the conversation, so a
    # FAILED generation leaves a visible session instead of silence.
    agent = _agent()
    transcript = [{"role": "user", "parts": [{"type": "text", "text": "make a point"}]}]
    fake = _FakeEngine(
        agent.submit_answer, [{"x": 1}], transcript=transcript, session_id="sess-2"
    )  # {"x": 1} never validates
    monkeypatch.setattr(agent, "_build_engine", lambda: fake)
    with pytest.raises(GenerationError):
        asyncio.run(agent.run())
    assert agent.transcript == transcript
    assert agent.session_id == "sess-2"
