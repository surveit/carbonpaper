"""get_llm_call_type() decision matrix — fully hermetic via monkeypatch.

Never touches the SDK's real availability or the real CLI path; both are patched
so the table is deterministic on any machine. The key policy: a real backend
that isn't available RAISES — we never silently fall back to the mock.
"""
from __future__ import annotations

import pytest

from app.runtime import options, llm_agent_sdk
from app.runtime.llm import call_llm


def _set(monkeypatch, *, force_mock=False, backend=None, sdk_available=False, cli=None):
    if force_mock:
        monkeypatch.setenv("CW_LLM_FORCE_MOCK", "1")
    else:
        monkeypatch.delenv("CW_LLM_FORCE_MOCK", raising=False)
    if backend is None:
        monkeypatch.delenv("CW_LLM_BACKEND", raising=False)
    else:
        monkeypatch.setenv("CW_LLM_BACKEND", backend)
    monkeypatch.setattr(llm_agent_sdk, "available", lambda: sdk_available)
    monkeypatch.setattr(options, "CLAUDE_BIN", cli)


def test_force_mock_overrides_everything(monkeypatch):
    _set(monkeypatch, force_mock=True, backend="agent_sdk", sdk_available=True, cli="/x")
    assert options.get_llm_call_type() == "mock"


def test_explicit_mock(monkeypatch):
    _set(monkeypatch, backend="mock", sdk_available=True, cli="/x")
    assert options.get_llm_call_type() == "mock"


def test_backend_cli_available(monkeypatch):
    _set(monkeypatch, backend="cli", sdk_available=True, cli="/usr/bin/claude")
    assert options.get_llm_call_type() == "cli"


def test_backend_cli_unavailable_raises(monkeypatch):
    # No silent mock fallback: an explicit cli request with no CLI must raise.
    _set(monkeypatch, backend="cli", sdk_available=True, cli=None)
    with pytest.raises(options.LLMError):
        options.get_llm_call_type()


@pytest.mark.parametrize("avail,cli,expected", [
    (True, "/x", "agent_sdk"),
    (False, "/x", "cli"),
])
def test_backend_agent_sdk_degrades_to_cli(monkeypatch, avail, cli, expected):
    _set(monkeypatch, backend="agent_sdk", sdk_available=avail, cli=cli)
    assert options.get_llm_call_type() == expected


def test_backend_agent_sdk_no_live_backend_raises(monkeypatch):
    _set(monkeypatch, backend="agent_sdk", sdk_available=False, cli=None)
    with pytest.raises(options.LLMError):
        options.get_llm_call_type()


@pytest.mark.parametrize("avail,cli,expected", [
    (True, "/x", "agent_sdk"),
    (False, "/x", "cli"),
])
def test_auto_prefers_sdk_then_cli(monkeypatch, avail, cli, expected):
    _set(monkeypatch, backend=None, sdk_available=avail, cli=cli)
    assert options.get_llm_call_type() == expected


def test_auto_no_live_backend_raises(monkeypatch):
    # The behavior change: auto no longer silently falls back to the mock.
    _set(monkeypatch, backend=None, sdk_available=False, cli=None)
    with pytest.raises(options.LLMError):
        options.get_llm_call_type()


def test_call_llm_takes_llm_config_model():
    from app.models import LLMConfig
    cfg = LLMConfig(prompt_template="score {name}")
    result = call_llm("some_stage", cfg, {"name": "acme"})
    assert result is not None
