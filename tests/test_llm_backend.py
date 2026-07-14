"""get_llm_call_type() decision matrix — fully hermetic via monkeypatch.

The key policy: a live backend that isn't available RAISES — we never silently
fall back to the mock. `mock` is reachable only when explicitly requested.
"""
from __future__ import annotations

import pytest

from app.core.errors import LLMError
from app.runtime import options


def _set(monkeypatch, *, force_mock=False, backend=None, agent=False):
    if force_mock:
        monkeypatch.setenv("CW_LLM_FORCE_MOCK", "1")
    else:
        monkeypatch.delenv("CW_LLM_FORCE_MOCK", raising=False)
    if backend is None:
        monkeypatch.delenv("CW_LLM_BACKEND", raising=False)
    else:
        monkeypatch.setenv("CW_LLM_BACKEND", backend)
    monkeypatch.setattr(options, "agent_available", lambda: agent)


def test_force_mock_overrides_everything(monkeypatch):
    _set(monkeypatch, force_mock=True, backend="agent", agent=True)
    assert options.get_llm_call_type() == "mock"


def test_explicit_mock(monkeypatch):
    _set(monkeypatch, backend="mock", agent=True)
    assert options.get_llm_call_type() == "mock"


def test_auto_picks_agent_when_available(monkeypatch):
    _set(monkeypatch, agent=True)
    assert options.get_llm_call_type() == "agent"


def test_auto_without_agent_raises_never_mocks(monkeypatch):
    _set(monkeypatch, agent=False)
    with pytest.raises(LLMError):
        options.get_llm_call_type()


def test_explicit_agent_unavailable_raises(monkeypatch):
    _set(monkeypatch, backend="agent", agent=False)
    with pytest.raises(LLMError):
        options.get_llm_call_type()


def test_unknown_backend_value_raises(monkeypatch):
    _set(monkeypatch, backend="cli", agent=True)
    with pytest.raises(LLMError):
        options.get_llm_call_type()
