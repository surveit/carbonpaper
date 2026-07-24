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

from pathlib import Path

import pandas as pd
import pytest

from app.runtime.context import (
    RunContext,
    RunIdentity,
    RunMode,
)
from app.runtime.manifest import CONTRIBUTION_ATTR, StageContribution
from app.core.stage_cache import ReadOnlyStageCache


def contribution_of(frame: pd.DataFrame) -> StageContribution:
    """The StageContribution a handler attached to its output frame's `.attrs`.
    A handler reports its usage/errors/dropped-columns/queue tallies here (the
    executor merges it into the manifest), so a direct-handler test reads them
    off the returned frame rather than off the context."""
    return frame.attrs[CONTRIBUTION_ATTR]


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    monkeypatch.setattr("app.runtime.options.agent_available", lambda: False)


@pytest.fixture(autouse=True)
def fresh_store():
    from app.core.persistence import SqliteKvStore, configure_store
    configure_store(SqliteKvStore(":memory:"))


@pytest.fixture(autouse=True)
def reset_cancellation_registry():
    """The cancel registry is process-global and production never removes keys
    (see app.runtime.cancellation), so reset it around each test to keep runs
    independent."""
    from app.runtime.cancellation import reset
    reset()
    yield
    reset()


def make_run_context(
    *,
    repo_root: Path = Path("."),
    run_dir: Path = Path("."),
    identity: RunIdentity | None = None,
    stage_cache: ReadOnlyStageCache | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
) -> RunContext:
    """A RunContext for tests that only care about a few of its fields. `mode`
    follows project scope: an `identity` (with its `stage_cache`) makes it a
    production run, otherwise non-production. A stage's telemetry is reported on
    its output frame's `.attrs`, not on the context, so there is nothing to seed
    here."""
    mode: RunMode = "production" if identity is not None else "non_production"
    return RunContext(
        mode=mode, repo_root=repo_root, run_dir=run_dir,
        identity=identity, stage_cache=stage_cache,
        limits=dict(limits or {}), offsets=dict(offsets or {}),
    )
