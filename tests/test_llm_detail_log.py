"""The LLM detail tier of the run log (app/runtime/llm.py).

Under a bound sink an agent call logs its prompt and forwards thinking/response
as LEVEL_DETAIL keyed to that (stage, rows); with none bound, nothing extra.
`call_llm`/`call_llm_batch` share `_run_agent`'s one emit point.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.models.stages.llm_transform import LLMConfig
from app.runtime import llm as llm_module
from app.runtime import options
from app.runtime.run_log import (
    LEVEL_DETAIL,
    RunLog,
    bind_detail_sink,
    bind_row_sink,
    read_events_since,
    unbind_detail_sink,
)


class _Reply(BaseModel):
    score: int


class _FakeAgent:
    """Streams a couple of events to the emit sink, then returns a valid reply."""

    last_usage = None

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def run(self, emit=None):
        if emit is not None:
            emit({"kind": "thinking", "text": "weighing the options"})
            emit({"kind": "tool_call", "name": "submit_answer",
                  "args": '{"score": 5}', "label": "submit answer"})
            emit({"kind": "tool_result", "content": "Accepted — recorded."})
        return _Reply(score=5)


def _use_fake_agent(monkeypatch) -> None:
    monkeypatch.setattr(options, "agent_available", lambda: True)
    monkeypatch.setattr(llm_module, "Agent", _FakeAgent)


def _events(path: Path) -> list[dict[str, Any]]:
    return read_events_since(path, 0)


def _llm_events(path: Path) -> dict[str, dict[str, Any]]:
    return {e["kind"]: e for e in _events(path) if e["kind"].startswith("llm_")}


def _call_one_row(tmp_path: Path, *, bind: bool) -> Path:
    path = tmp_path / "events.jsonl"
    log = RunLog(path)
    token = bind_row_sink(log, "classify", 3) if bind else None
    try:
        reply = llm_module.call_llm(
            "classify", LLMConfig(prompt_data_template="Rate: {text}"),
            {"text": "hello"}, reply_model=_Reply,
        )
    finally:
        if token is not None:
            unbind_detail_sink(token)
    log.close()
    assert reply == {"score": 5}
    return path


def test_detail_events_logged_when_a_row_is_bound(tmp_path, monkeypatch):
    _use_fake_agent(monkeypatch)

    by_kind = _llm_events(_call_one_row(tmp_path, bind=True))

    # Prompt, thinking, and the submitted response all land, at the detail tier,
    # attributed to the bound (stage, row).
    assert set(by_kind) == {
        "llm_prompt", "llm_thinking", "llm_response", "llm_tool_result"
    }
    for event in by_kind.values():
        assert event["level"] == LEVEL_DETAIL
        assert event["stage"] == "classify"
        assert event["row"] == 3 and event["rows"] == [3]
    assert by_kind["llm_prompt"]["text"] == "Rate: hello"     # rendered, not the template
    assert by_kind["llm_thinking"]["text"] == "weighing the options"
    assert by_kind["llm_response"]["text"] == '{"score": 5}'  # the submit_answer args
    # The tool's verdict on that submission. It is the only place an upstream
    # rejection of a wrong-shaped call is visible — the handler never runs, so
    # nothing else in the tier records that the call happened and was refused.
    assert by_kind["llm_tool_result"]["text"] == "Accepted — recorded."


def test_no_detail_events_without_a_bound_row(tmp_path, monkeypatch):
    _use_fake_agent(monkeypatch)

    assert _llm_events(_call_one_row(tmp_path, bind=False)) == {}


def test_a_batched_call_logs_its_chunk_prompt_against_every_row_it_covers(
    tmp_path, monkeypatch
):
    _use_fake_agent(monkeypatch)
    path = tmp_path / "events.jsonl"
    log = RunLog(path)
    token = bind_detail_sink(log, "classify", (4, 5, 6))
    try:
        llm_module.call_llm_batch(
            "classify", LLMConfig(prompt_data_template="{text}"),
            instructions="score them", task="0. a\n1. b\n2. c",
            reply_schema=_Reply,
        )
    finally:
        unbind_detail_sink(token)
    log.close()

    prompt = _llm_events(path)["llm_prompt"]
    assert prompt["text"] == "0. a\n1. b\n2. c"
    assert prompt["level"] == LEVEL_DETAIL
    # One prompt covered three rows: `rows` says so rather than the chunk's
    # detail reading as if it belonged to row 4 alone.
    assert prompt["row"] == 4 and prompt["rows"] == [4, 5, 6]
