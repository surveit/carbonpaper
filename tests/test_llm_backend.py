"""Backend availability policy — fully hermetic via monkeypatch.

The key policy: the structured-output agent is the ONLY backend and there is
no fallback. When it isn't available (claude-agent-sdk not importable, or no
claude CLI located), the runtime raises rather than fabricating a reply.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.core.errors import LLMError
from app.core.models import LLMConfig
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
