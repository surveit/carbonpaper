"""RunContext: the typed run context every stage handler receives instead of
the old `ctx: dict[str, Any]`. Pins the two structural facts Task 3 exists to
establish: no field on it names a project directory, and identity/stage_cache
are granted (or withheld) together, never independently."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.core.run_status import RunMode
from app.runtime.context import RunContext, RunIdentity
from app.services.stage_cache import StageCacheEntry


def _make(**overrides: object) -> RunContext:
    defaults: dict[str, object] = dict(
        repo_root=Path("."), run_dir=Path("."), identity=None, stage_cache=None,
        limits={}, offsets={},
    )
    defaults.update(overrides)
    return RunContext(**defaults)  # type: ignore[arg-type]


def test_run_context_has_no_project_scope_path() -> None:
    """No field on RunContext names a project directory — a handler that only
    receives a RunContext structurally cannot reach one. (Red-proved during
    development: temporarily adding a `project_dir: Path | None = None` field
    to RunContext turns this red; removing it turns it green again.)"""
    field_names = {f.name for f in dataclasses.fields(RunContext)}
    banned = {"project_dir", "project_path", "project_root"}
    assert not (field_names & banned)


def test_run_context_accepts_no_identity_and_no_cache() -> None:
    ctx = _make()
    assert ctx.identity is None
    assert ctx.stage_cache is None


def test_run_context_accepts_identity_with_cache() -> None:
    identity = RunIdentity(project="p", run_id="r1")
    ctx = _make(identity=identity, stage_cache=StageCacheEntry.for_mode(RunMode.PRODUCTION))
    assert ctx.identity == identity
    assert ctx.stage_cache is not None


def test_run_context_rejects_identity_without_cache() -> None:
    with pytest.raises(ValueError, match="both be set or both be None"):
        _make(identity=RunIdentity(project="p", run_id="r1"), stage_cache=None)


def test_run_context_rejects_cache_without_identity() -> None:
    with pytest.raises(ValueError, match="both be set or both be None"):
        _make(identity=None, stage_cache=StageCacheEntry.for_mode(RunMode.PRODUCTION))


def test_run_context_telemetry_accumulators_default_empty_and_independent() -> None:
    """default_factory=dict on every accumulator field: two RunContext
    instances must not share the same underlying dict."""
    a = _make()
    b = _make()
    a.row_errors["x"] = [{"row": 0, "message": "boom"}]
    assert b.row_errors == {}
    assert a.queue_stats == {} and a.dropped_columns == {} and a.llm_usage == {} and a.llm_backend == {}
