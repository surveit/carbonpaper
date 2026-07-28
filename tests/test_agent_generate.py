from __future__ import annotations

import asyncio
import json
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
    next scripted call represents.

    `events` are emitted verbatim, in the real engine's event shapes, BEFORE the scripted
    handler calls run. That separation is the point: the model can emit tool_call events
    that never reach the handler, which is the failure this file's diagnostics tests
    reproduce."""

    def __init__(
        self,
        submit: Callable[..., str],
        scripted_calls: list[dict[str, Any]],
        *,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self._submit = submit
        self._scripted = scripted_calls
        self._events = events or []

    async def stream_turn(
        self, task: str, *, message_history: Any, emit: Any, resume: Any
    ) -> tuple[list[Any], None]:
        for event in self._events:
            emit(event)
        for fields in self._scripted:
            try:
                self._submit(**fields)
            except ValueError:
                pass  # rejected submission; the agent would fix + retry (next call)
        return [], None


def _tool_call_event(**args: Any) -> dict[str, Any]:
    return {"kind": "tool_call", "name": "submit_answer", "args": json.dumps(args),
            "label": "submit_answer"}


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


def _failure_message(monkeypatch: Any, fake_factory: Callable[[Any], _FakeEngine]) -> str:
    agent = _agent()
    monkeypatch.setattr(agent, "build_engine", lambda: fake_factory(agent))
    with pytest.raises(GenerationError) as exc_info:
        asyncio.run(agent.run())
    return str(exc_info.value)


def test_failure_separates_calls_the_model_emitted_from_calls_the_handler_saw(
    monkeypatch: Any,
) -> None:
    # #208's ambiguity: the handler counter alone cannot tell "the model never called the
    # tool" from "the model called it and something upstream rejected the call". Here the
    # model emits two tool_calls that never reach the handler, and the tool_result carries
    # the upstream rejection — the message must show both numbers and that text.
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(
            agent.submit_answer,
            [],
            events=[
                _tool_call_event(x=1, y="two"),
                {"kind": "tool_result", "content": "input validation error: y must be integer"},
                _tool_call_event(x=1, y="also two"),
                {"kind": "tool_result", "content": "input validation error: y must be integer"},
            ],
        ),
    )
    assert "2 tool_call event(s)" in message      # what the model emitted
    assert "0 reached the handler" in message     # what our Python function saw
    assert "input validation error: y must be integer" in message


def test_failure_shows_a_tool_less_completion_as_zero_calls_and_no_results(
    monkeypatch: Any,
) -> None:
    # The other hypothesis: the model really never called the tool and answered in prose.
    # The prose itself is the run log's job (llm_text); what the message must show is that
    # nothing was emitted, stated as absent rather than left to be inferred.
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(
            agent.submit_answer,
            [],
            events=[{"kind": "text", "text": "The point is x=1, y=2."}],
        ),
    )
    assert "0 tool_call event(s)" in message
    assert "0 reached the handler" in message
    assert "tool results: (none emitted)" in message
    assert "terminal error: (none emitted)" in message


def test_failure_reports_the_terminal_error_the_turn_ended_with(monkeypatch: Any) -> None:
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(
            agent.submit_answer,
            [],
            events=[{"kind": "error", "text": "agent run failed: max turns exceeded"}],
        ),
    )
    assert "terminal error: agent run failed: max turns exceeded" in message


def test_failure_announces_a_truncated_tool_result(monkeypatch: Any) -> None:
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(
            agent.submit_answer,
            [],
            events=[{"kind": "tool_result", "content": "z" * 5000}],
        ),
    )
    assert "truncated: 800 of 5000 chars shown" in message
    assert "z" * 800 in message
    assert "z" * 801 not in message


def test_failure_still_reports_the_handler_issues_of_a_rejected_submission(
    monkeypatch: Any,
) -> None:
    # Handler ran and rejected: both counters agree, and the pydantic issues survive.
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(
            agent.submit_answer, [{"x": 1}], events=[_tool_call_event(x=1)]
        ),
    )
    assert "1 tool_call event(s)" in message
    assert "1 reached the handler" in message
    assert "y" in message  # the missing-field issue from submit_answer


def test_run_forwards_events_to_an_opted_in_caller_and_still_summarizes(
    monkeypatch: Any,
) -> None:
    # The runtime's detail log opts in via `emit`; collecting for the failure summary
    # must not consume the stream out from under it.
    agent = _agent()
    forwarded: list[dict[str, Any]] = []
    events = [_tool_call_event(x=1), {"kind": "tool_result", "content": "rejected"}]
    monkeypatch.setattr(
        agent, "build_engine", lambda: _FakeEngine(agent.submit_answer, [], events=events)
    )
    with pytest.raises(GenerationError) as exc_info:
        asyncio.run(agent.run(forwarded.append))
    assert forwarded == events                       # the caller saw every event
    assert "rejected" in str(exc_info.value)         # and so did the summary


def test_answer_exposes_the_captured_submission_for_live_driving(monkeypatch: Any) -> None:
    # When driven as a live turn, the caller reads `answer` after the turn (rather than
    # run()'s return value) to persist the result.
    agent = _agent()
    assert agent.answer is None
    agent.submit_answer(x=3, y=4)
    assert agent.answer == _Point(x=3, y=4)
