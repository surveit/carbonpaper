"""Backend availability policy — fully hermetic via monkeypatch.

The key policy: the structured-output agent is the ONLY backend and there is
no fallback. When it isn't available (claude-agent-sdk not importable, or no
claude CLI located), the runtime raises rather than fabricating a reply.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.core.errors import LLMError
from app.models import LLMConfig
from app.runtime import llm as llm_module
from app.runtime import options


class _Reply(BaseModel):
    score: int


def test_available_backend_passes(monkeypatch):
    monkeypatch.setattr(options, "agent_available", lambda: True)
    options.require_agent_backend()  # does not raise


def test_unavailable_backend_raises(monkeypatch):
    monkeypatch.setattr(options, "agent_available", lambda: False)
    with pytest.raises(LLMError):
        options.require_agent_backend()


def test_call_llm_without_backend_raises(monkeypatch):
    """`call_llm` refuses to run without a live backend — no fallback reply."""
    monkeypatch.setattr(options, "agent_available", lambda: False)
    config = LLMConfig(prompt_template="Rate: {text}")
    with pytest.raises(LLMError):
        llm_module.call_llm("stage", config, {"text": "hi"}, reply_model=_Reply)


def test_call_llm_with_tools_raises_before_running_agent(monkeypatch):
    """`llm.tools` is not supported by the agent backend: `call_llm` must reject
    it up front, without constructing or running an `Agent`."""
    monkeypatch.setattr(options, "agent_available", lambda: True)

    config = LLMConfig(prompt_template="Rate: {text}", tools=["WebSearch"])
    with pytest.raises(LLMError):
        llm_module.call_llm("stage", config, {"text": "hi"}, reply_model=_Reply)


def test_backend_status_reports_unavailability(monkeypatch):
    monkeypatch.setattr(options, "agent_available", lambda: False)
    status = llm_module.backend_status()
    assert status["backend"] is None
    assert "No LLM backend available" in status["backend_error"]


class _FakeAgent:
    """Stand-in for `app.core.agent.agent.Agent`: records the constructor
    kwargs `call_llm` built (system_prompt, task) and returns a fixed reply
    without touching a live backend."""

    captured: list[dict[str, object]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeAgent.captured.append(kwargs)

    async def run(self):
        return _Reply(score=1)


def _stub_agent(monkeypatch):
    monkeypatch.setattr(options, "agent_available", lambda: True)
    _FakeAgent.captured = []
    monkeypatch.setattr(llm_module, "Agent", _FakeAgent)
    return _FakeAgent


def test_prompt_instructions_identical_across_rows(monkeypatch):
    """`prompt_instructions` is row-invariant: it must land in the system
    prompt unchanged regardless of which row is processed."""
    fake = _stub_agent(monkeypatch)
    config = LLMConfig(
        prompt_instructions="Always cite your source.",
        prompt_data_template="Rate: {text}",
    )
    llm_module.call_llm("stage", config, {"text": "row one"}, reply_model=_Reply)
    llm_module.call_llm("stage", config, {"text": "row two"}, reply_model=_Reply)

    system_prompts = [kwargs["system_prompt"] for kwargs in fake.captured]
    assert len(system_prompts) == 2
    assert system_prompts[0] == system_prompts[1]
    assert system_prompts[0] == llm_module.SYSTEM_PROMPT + "\n\nAlways cite your source."


def test_system_prompt_composes_instructions(monkeypatch):
    """Non-empty `prompt_instructions` is appended to SYSTEM_PROMPT with a
    blank-line separator; empty `prompt_instructions` leaves SYSTEM_PROMPT
    unchanged (Task 1 default)."""
    fake = _stub_agent(monkeypatch)
    config = LLMConfig(
        prompt_instructions="Be terse.",
        prompt_data_template="Rate: {text}",
    )
    llm_module.call_llm("stage", config, {"text": "hi"}, reply_model=_Reply)
    assert fake.captured[-1]["system_prompt"] == llm_module.SYSTEM_PROMPT + "\n\nBe terse."

    fake.captured = []
    empty_config = LLMConfig(prompt_data_template="Rate: {text}")
    llm_module.call_llm("stage", empty_config, {"text": "hi"}, reply_model=_Reply)
    assert fake.captured[-1]["system_prompt"] == llm_module.SYSTEM_PROMPT


def test_only_data_template_is_rendered(monkeypatch):
    """`prompt_instructions` is never passed through `render_prompt` /
    `str.format_map`: a `{placeholder}` inside it must NOT be rendered against
    the row, and must not raise even when it doesn't name a row column."""
    fake = _stub_agent(monkeypatch)
    config = LLMConfig(
        prompt_instructions="Follow the house style for {nonexistent_column}.",
        prompt_data_template="Rate: {text}",
    )
    llm_module.call_llm("stage", config, {"text": "hi"}, reply_model=_Reply)

    kwargs = fake.captured[-1]
    assert kwargs["task"] == "Rate: hi"
    assert "Follow the house style for {nonexistent_column}." in kwargs["system_prompt"]
