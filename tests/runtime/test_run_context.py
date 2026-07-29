from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.runtime.context import RunContext, RunIdentity
from app.core.stage_cache import StageCacheEntry


def _make(**overrides: object) -> RunContext:
    identity = overrides.get("identity")
    defaults: dict[str, object] = dict(
        mode="production" if identity is not None else "non_production",
        repo_root=Path("."), run_dir=Path("."), identity=None, stage_cache=None,
        limits={}, offsets={},
    )
    defaults.update(overrides)
    return RunContext(**defaults)  # type: ignore[arg-type]


def test_run_context_has_no_project_scope_path() -> None:
    """No field on RunContext names a project directory — a handler that only
    receives a RunContext structurally cannot reach one. Project scope is the
    (project, run_id) identity plus the stage cache, never a directory."""
    banned = {"project_dir", "project_path", "project_root"}
    assert not (set(RunContext.model_fields) & banned)


def test_run_context_accepts_no_identity_and_no_cache() -> None:
    ctx = _make()
    assert ctx.identity is None
    assert ctx.stage_cache is None


def test_run_context_accepts_identity_with_cache() -> None:
    identity = RunIdentity(project="p", run_id="r1")
    ctx = _make(identity=identity, stage_cache=StageCacheEntry.read_write())
    assert ctx.identity == identity
    assert ctx.stage_cache is not None


def test_run_context_rejects_identity_without_cache() -> None:
    with pytest.raises(ValidationError, match="both be set or both be None"):
        _make(identity=RunIdentity(project="p", run_id="r1"), stage_cache=None)


def test_run_context_rejects_cache_without_identity() -> None:
    with pytest.raises(ValidationError, match="both be set or both be None"):
        _make(identity=None, stage_cache=StageCacheEntry.read_write())


def test_production_run_context_rejects_queue_auto_approve(tmp_path: Path) -> None:
    """A production run must never carry the in-memory queue bypass: pairing
    mode='production' with queue_auto_approve fails loudly at construction."""
    with pytest.raises(ValidationError, match="non-production-run bypass"):
        RunContext(
            mode="production",
            repo_root=tmp_path,
            run_dir=tmp_path / "run",
            queue_auto_approve=True,
        )


def test_non_production_run_context_allows_queue_auto_approve(tmp_path: Path) -> None:
    """A non-production run may set the bypass — that is the only mode that can."""
    ctx = RunContext(
        mode="non_production",
        repo_root=tmp_path,
        run_dir=tmp_path / "run",
        queue_auto_approve=True,
    )
    assert ctx.queue_auto_approve is True


def test_run_context_without_a_cache_rejects_bust_cache(tmp_path: Path) -> None:
    """Busting the cache is meaningful only for a run that HAS one: a context
    with no stage cache paired with bust_cache fails loudly at construction."""
    with pytest.raises(ValidationError, match="no stage cache to bust"):
        RunContext(
            mode="non_production",
            repo_root=tmp_path,
            run_dir=tmp_path / "run",
            bust_cache=True,
        )


def test_bust_cache_defaults_off() -> None:
    assert _make().bust_cache is False


def test_run_context_is_frozen(tmp_path: Path) -> None:
    """Identity/config is immutable once built — nothing mutates it mid-run."""
    ctx = _make()
    with pytest.raises(ValidationError):
        ctx.run_dir = tmp_path / "other"  # type: ignore[misc]


def test_for_production_run_stamps_mode_and_grants_scope(tmp_path: Path) -> None:
    ctx = RunContext.for_production_run(tmp_path, tmp_path / "run", "proj", "r1")
    assert ctx.mode == "production"
    assert ctx.identity == RunIdentity(project="proj", run_id="r1")
    assert ctx.stage_cache is not None
    assert ctx.bust_cache is False


def test_for_production_run_carries_bust_cache(tmp_path: Path) -> None:
    ctx = RunContext.for_production_run(
        tmp_path, tmp_path / "run", "proj", "r1", bust_cache=True)
    assert ctx.bust_cache is True
    assert ctx.stage_cache is not None  # still write-capable: re-pinned, not stale


def test_for_non_production_run_takes_no_bust_cache() -> None:
    """A non-production run's cache, when it has one at all, is read-only — so
    the constructor offers no way to ask for one to be busted."""
    assert "bust_cache" not in inspect.signature(
        RunContext.for_non_production_run).parameters


def test_for_non_production_run_with_a_project_grants_read_only_scope(tmp_path: Path) -> None:
    """A workflow test's context: still non-production, but scoped — so a publish
    stage's trace_links resolves and a slow stage can replay a cached result. The
    cache view cannot record, so the test can never write what production reads."""
    ctx = RunContext.for_non_production_run(
        tmp_path, tmp_path / "run", project="proj", run_id="r1")
    assert ctx.mode == "non_production"
    assert ctx.identity == RunIdentity(project="proj", run_id="r1")
    assert ctx.stage_cache is not None
    assert not hasattr(ctx.stage_cache, "record")


def test_for_non_production_run_rejects_half_an_identity(tmp_path: Path) -> None:
    """project and run_id are two halves of one identity: neither alone can key a
    cache entry, so passing one is a loud error, not a silently unscoped run."""
    with pytest.raises(ValueError, match="two halves of one identity"):
        RunContext.for_non_production_run(tmp_path, tmp_path / "run", project="proj")
    with pytest.raises(ValueError, match="two halves of one identity"):
        RunContext.for_non_production_run(tmp_path, tmp_path / "run", run_id="r1")


def test_for_non_production_run_allows_none_paths() -> None:
    """The in-memory harness context carries no on-disk roots; require_run_dir
    fails loudly rather than handing back a fabricated path."""
    ctx = RunContext.for_non_production_run(None, None)
    assert ctx.mode == "non_production"
    assert ctx.identity is None and ctx.stage_cache is None
    with pytest.raises(ValueError, match="no run_dir"):
        ctx.require_run_dir()
