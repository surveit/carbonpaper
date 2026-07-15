"""Shared pytest fixtures.

Tests never reach a live LLM: `agent_available` is forced False, so any
un-stubbed `call_llm` raises `LLMError` instead of shelling out to the real
`claude` CLI. A test that exercises the LLM boundary monkeypatches `call_llm`
(or `agent_available`) itself.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    monkeypatch.setattr("app.runtime.options.agent_available", lambda: False)
