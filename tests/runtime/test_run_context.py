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


def test_a_writable_cache_rejects_queue_auto_approve(tmp_path: Path) -> None:
    """The bypass approves in memory; a WRITE-capable cache would persist those
    approvals for a later run to read back as human decisions. Loud at construction."""
    with pytest.raises(ValidationError, match="stage cache is WRITABLE"):
        RunContext(
            repo_root=tmp_path,
            run_dir=tmp_path / "run",
            identity=RunIdentity(project="p", run_id="r1"),
            stage_cache=StageCacheEntry.read_write(),
            queue_auto_approve=True,
        )


def test_a_read_only_cache_allows_queue_auto_approve(tmp_path: Path) -> None:
    """A workflow test's shape: scoped, but read-only, so its in-memory approvals
    cannot be persisted — the bypass is safe and permitted."""
    ctx = RunContext(
        repo_root=tmp_path,
        run_dir=tmp_path / "run",
        identity=RunIdentity(project="p", run_id="r1"),
        stage_cache=StageCacheEntry.read_only(),
        queue_auto_approve=True,
    )
    assert ctx.queue_auto_approve is True


def test_no_cache_at_all_allows_queue_auto_approve(tmp_path: Path) -> None:
    """Stages outside a run have no cache to persist an approval into."""
    ctx = RunContext(
        repo_root=tmp_path, run_dir=tmp_path / "run", queue_auto_approve=True)
    assert ctx.queue_auto_approve is True


def test_run_context_without_a_cache_rejects_bust_cache(tmp_path: Path) -> None:
    """Busting the cache is meaningful only for a run that HAS one: a context
    with no stage cache paired with bust_cache fails loudly at construction."""
    with pytest.raises(ValidationError, match="no stage cache to bust"):
        RunContext(
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


def test_for_workflow_run_grants_read_write_scope(tmp_path: Path) -> None:
    ctx = RunContext.for_workflow_run(tmp_path, tmp_path / "run", "proj", "r1")
    assert hasattr(ctx.stage_cache, "record")  # the only constructor that can write
    assert ctx.identity == RunIdentity(project="proj", run_id="r1")
    assert ctx.stage_cache is not None
    assert ctx.bust_cache is False


def test_for_workflow_run_carries_bust_cache(tmp_path: Path) -> None:
    ctx = RunContext.for_workflow_run(
        tmp_path, tmp_path / "run", "proj", "r1", bust_cache=True)
    assert ctx.bust_cache is True
    assert ctx.stage_cache is not None  # still write-capable: re-pinned, not stale


def test_only_a_workflow_run_takes_bust_cache() -> None:
    """Busting re-pins what it skips, which needs a writable cache — so only the
    one constructor that grants one offers the argument."""
    assert "bust_cache" in inspect.signature(RunContext.for_workflow_run).parameters
    assert "bust_cache" not in inspect.signature(
        RunContext.for_workflow_test_run).parameters
    assert "bust_cache" not in inspect.signature(
        RunContext.for_stages_outside_a_run).parameters


def test_for_workflow_test_run_grants_scope_but_read_only(tmp_path: Path) -> None:
    """A workflow test gets the same scope a workflow run does — so trace_links
    resolves and a slow stage can replay a cached result — but the cache view
    carries no `record`, so it can never write what a workflow run reads back."""
    ctx = RunContext.for_workflow_test_run(tmp_path, tmp_path / "run", "proj", "r1")
    assert ctx.identity == RunIdentity(project="proj", run_id="r1")
    assert ctx.stage_cache is not None
    assert not hasattr(ctx.stage_cache, "record")
    assert ctx.queue_auto_approve is True  # safe: the read-only cache can't persist it


def test_for_stages_outside_a_run_allows_none_paths() -> None:
    """No run on disk at all; require_run_dir fails loudly rather than handing
    back a fabricated path."""
    ctx = RunContext.for_stages_outside_a_run(None, None)
    assert ctx.identity is None and ctx.stage_cache is None
    with pytest.raises(ValueError, match="no run_dir"):
        ctx.require_run_dir()
