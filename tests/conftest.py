"""Shared pytest fixtures.

Tests never reach a live LLM: `agent_available` is forced False, so any
un-stubbed `call_llm` raises `LLMError` instead of shelling out to the real
`claude` CLI. A test that exercises the LLM boundary monkeypatches `call_llm`
(or `agent_available`) itself.

Every test also gets a fresh in-memory document store (app.core.persistence),
isolating it from other tests and from any on-disk database. This runs ahead of
the app's own startup wiring, so `app.main`'s lifespan — guarded by
`is_store_configured()` — finds a store already configured and leaves it alone.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    monkeypatch.setattr("app.runtime.options.agent_available", lambda: False)


@pytest.fixture(autouse=True)
def fresh_store():
    from app.core.persistence import configure_store
    from app.core.sqlite_store import SqliteKvStore
    configure_store(SqliteKvStore(":memory:"))
