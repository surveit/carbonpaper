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


# The tool list the CLI actually advertised in the failing live-llm run of CI run
# 30549321524 (claude_code_version 2.1.195), read off that run's events.jsonl. The
# built-ins are the drift surface a one-shot structured-output turn must not be shown.
_CI_ADVERTISED_TOOLS = [
    "Task", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync", "Edit",
    "EnterWorktree", "ExitWorktree", "Monitor", "NotebookEdit", "PushNotification",
    "Read", "RemoteTrigger", "ScheduleWakeup", "SendMessage", "Skill", "TaskCreate",
    "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate", "ToolSearch",
    "WebFetch", "WebSearch", "Workflow", "Write", "mcp__tools__submit_answer",
]


def _init_event(*, tools: list[str], text: str | None = None) -> dict[str, Any]:
    """One CLI init as the ENGINE emits it: kind "system", subtype "init", and the
    inventory as the JSON body of `text` (matching sdk_engine.stream_turn)."""
    body = json.dumps({
        "type": "system", "subtype": "init",
        "session_id": "bb443cc3-9afb-4dfb-844d-581e04d61679",
        "tools": tools,
        "mcp_servers": [{"name": "tools", "status": "connected"}],
        "model": "claude-haiku-4-5-20251001",
        "claude_code_version": "2.1.195",
    })
    return {"kind": "system", "subtype": "init", "text": body if text is None else text}


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


def test_failure_reads_the_tool_inventory_out_of_the_engines_init_event(
    monkeypatch: Any,
) -> None:
    # The init arrives as kind "system" / subtype "init" with the inventory inside its
    # JSON `text`; a reader that matches on kind "init" sees nothing and reports every
    # failure as "no init". Fixture shape and tool list come from the real CI artifact.
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(
            agent.submit_answer,
            [],
            events=[
                _init_event(tools=_CI_ADVERTISED_TOOLS),
                {"kind": "text", "text": "This statement is about money."},
            ],
        ),
    )
    assert "submit_answer advertised=yes" in message
    assert "mcp servers: tools=connected" in message
    assert "no init reported" not in message


def test_failure_reports_a_tool_the_init_never_advertised(monkeypatch: Any) -> None:
    # advertised=NO is the environment fault (the MCP server did not connect), which
    # must not read the same as the model declining to call a tool it was offered.
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(
            agent.submit_answer, [], events=[_init_event(tools=["Bash", "Read"])]
        ),
    )
    assert "submit_answer advertised=NO" in message


def test_failure_counts_an_unreadable_init_instead_of_reading_it_as_absent(
    monkeypatch: Any,
) -> None:
    # This code runs while rendering an error, so a truncated body must not raise —
    # and must not silently become "no init", the reading that hides an init entirely.
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(
            agent.submit_answer,
            [],
            events=[_init_event(tools=[], text='{"tools": ["Bash"')],
        ),
    )
    assert "1 init(s) unreadable" in message
    assert "no init reported" not in message


def test_failure_reports_no_init_when_the_engine_emitted_none(monkeypatch: Any) -> None:
    message = _failure_message(
        monkeypatch,
        lambda agent: _FakeEngine(agent.submit_answer, [], events=[]),
    )
    assert "tool availability: (no init reported)" in message


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
