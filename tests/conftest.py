"""Shared pytest fixtures.

Default every test to the deterministic offline mock backend so the suite never
shells out to the real `claude` CLI or touches the network. Tests that exercise
backend *selection* opt out with monkeypatch.delenv("CW_LLM_FORCE_MOCK").
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def force_mock_llm(monkeypatch):
    monkeypatch.setenv("CW_LLM_FORCE_MOCK", "1")
