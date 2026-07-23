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

import pytest

from app.runtime.context import RunContext, RunIdentity
from app.services.stage_cache import ReadOnlyStageCache


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


@pytest.fixture(autouse=True)
def reset_decisions_dir_registry():
    """The TRANSITIONAL project-dir registry human_review_queue uses to
    resolve its decisions directory (app.runtime.stages.human_review_queue)
    is process-global and production never removes keys, so reset it around
    each test to keep runs independent."""
    from app.runtime.stages.human_review_queue import reset_project_dirs
    reset_project_dirs()
    yield
    reset_project_dirs()


def make_run_context(
    *,
    repo_root: Path = Path("."),
    run_dir: Path = Path("."),
    identity: RunIdentity | None = None,
    stage_cache: ReadOnlyStageCache | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
) -> RunContext:
    """A RunContext for tests that only care about a few of its fields —
    telemetry accumulators (queue_stats, dropped_columns, row_errors,
    llm_usage, llm_backend) always start empty via RunContext's own
    defaults."""
    return RunContext(
        repo_root=repo_root, run_dir=run_dir, identity=identity, stage_cache=stage_cache,
        limits=dict(limits or {}), offsets=dict(offsets or {}),
    )
