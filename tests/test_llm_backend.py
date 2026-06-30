"""resolve_backend() decision matrix — fully hermetic via monkeypatch.

Never imports the SDK's real availability or the real CLI path; both are patched
so the table is deterministic on any machine.
"""
from __future__ import annotations

import pytest

from app.runtime import llm, llm_agent_sdk


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
    monkeypatch.setattr(llm, "CLAUDE_BIN", cli)


def test_force_mock_overrides_everything(monkeypatch):
    _set(monkeypatch, force_mock=True, backend="agent_sdk", sdk_available=True, cli="/x")
    assert llm.resolve_backend() == "mock"


def test_explicit_mock(monkeypatch):
    _set(monkeypatch, backend="mock", sdk_available=True, cli="/x")
    assert llm.resolve_backend() == "mock"


@pytest.mark.parametrize("cli,expected", [("/usr/bin/claude", "cli"), (None, "mock")])
def test_backend_cli(monkeypatch, cli, expected):
    _set(monkeypatch, backend="cli", sdk_available=True, cli=cli)
    assert llm.resolve_backend() == expected


@pytest.mark.parametrize("avail,cli,expected", [
    (True, "/x", "agent_sdk"),
    (False, "/x", "cli"),
    (False, None, "mock"),
])
def test_backend_agent_sdk_degrades_gracefully(monkeypatch, avail, cli, expected):
    _set(monkeypatch, backend="agent_sdk", sdk_available=avail, cli=cli)
    assert llm.resolve_backend() == expected


@pytest.mark.parametrize("avail,cli,expected", [
    (True, "/x", "agent_sdk"),
    (False, "/x", "cli"),
    (False, None, "mock"),
])
def test_auto_prefers_sdk_then_cli_then_mock(monkeypatch, avail, cli, expected):
    _set(monkeypatch, backend=None, sdk_available=avail, cli=cli)
    assert llm.resolve_backend() == expected
