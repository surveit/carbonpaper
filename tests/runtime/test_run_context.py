from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.runtime.context import RunContext, RunIdentity
from app.models.run_parameters import RunParameters
from app.models.review_ledger import ReviewLedger
from app.core.stage_cache import StageCacheEntry


def _make(**overrides: object) -> RunContext:
    defaults: dict[str, object] = dict(
        run_dir=Path("."), identity=None, stage_cache=None,
    )
    defaults.update(overrides)
    return RunContext(**defaults)  # type: ignore[arg-type]


def test_run_context_has_no_project_scope_path() -> None:
    """A handler holding only a RunContext must be structurally unable to reach a project directory."""
    banned = {"project_dir", "project_path", "project_root"}
    assert not (set(RunContext.model_fields) & banned)


def test_run_context_accepts_no_identity_and_no_cache() -> None:
    ctx = _make()
    assert ctx.identity is None
    assert ctx.stage_cache is None


def test_run_context_accepts_identity_with_cache() -> None:
    identity = RunIdentity(project="p", run_id="r1")
    ctx = _make(
        identity=identity, stage_cache=StageCacheEntry.read_write(),
        decisions=ReviewLedger("p"),
    )
    assert ctx.identity == identity
    assert ctx.stage_cache is not None


def test_run_context_rejects_identity_without_cache() -> None:
    with pytest.raises(ValidationError, match="all be set or all be None"):
        _make(identity=RunIdentity(project="p", run_id="r1"), stage_cache=None)


def test_run_context_rejects_cache_without_identity() -> None:
    with pytest.raises(ValidationError, match="all be set or all be None"):
        _make(identity=None, stage_cache=StageCacheEntry.read_write())


def _bypassing() -> RunParameters:
    return RunParameters(queue_auto_approve=True, is_test_run=True)


def test_a_writable_cache_rejects_queue_auto_approve(tmp_path: Path) -> None:
    """A writable cache would persist auto-approvals for a later run to read back as human decisions."""
    with pytest.raises(ValidationError, match="stage cache is WRITABLE"):
        RunContext(
            run_dir=tmp_path / "run",
            identity=RunIdentity(project="p", run_id="r1"),
            stage_cache=StageCacheEntry.read_write(),
            params=_bypassing(),
        )


def test_a_read_only_cache_allows_queue_auto_approve(tmp_path: Path) -> None:
    ctx = RunContext(
        run_dir=tmp_path / "run",
        identity=RunIdentity(project="p", run_id="r1"),
        stage_cache=StageCacheEntry.read_only(),
        decisions=ReviewLedger("p"),
        params=_bypassing(),
    )
    assert ctx.params.queue_auto_approve is True


def test_no_cache_at_all_allows_queue_auto_approve(tmp_path: Path) -> None:
    ctx = RunContext(
        run_dir=tmp_path / "run", params=_bypassing())
    assert ctx.params.queue_auto_approve is True


def test_run_context_without_a_cache_rejects_bust_cache(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not writable"):
        RunContext(
            run_dir=tmp_path / "run",
            params=RunParameters(bust_cache=True),
        )


def test_bust_cache_defaults_off() -> None:
    assert _make().params.bust_cache is False


def test_run_context_is_frozen(tmp_path: Path) -> None:
    ctx = _make()
    with pytest.raises(ValidationError):
        ctx.run_dir = tmp_path / "other"  # type: ignore[misc]


def test_for_workflow_run_grants_read_write_scope(tmp_path: Path) -> None:
    ctx = RunContext.for_workflow_run(tmp_path / "run", "proj", "r1")
    assert hasattr(ctx.stage_cache, "record")  # the only constructor that can write
    assert ctx.identity == RunIdentity(project="proj", run_id="r1")
    assert ctx.stage_cache is not None
    assert ctx.params.bust_cache is False


def test_for_workflow_run_carries_bust_cache(tmp_path: Path) -> None:
    ctx = RunContext.for_workflow_run(
        tmp_path / "run", "proj", "r1", RunParameters(bust_cache=True))
    assert ctx.params.bust_cache is True
    assert ctx.stage_cache is not None  # still write-capable: re-pinned, not stale


def test_only_a_run_with_a_writable_cache_may_bust_it(tmp_path: Path) -> None:
    for constructor in (RunContext.for_workflow_test_run,):
        with pytest.raises(ValidationError, match="not writable"):
            constructor(tmp_path / "run", "proj", "r1",
                        RunParameters(bust_cache=True))
    with pytest.raises(ValidationError, match="not writable"):
        RunContext.for_stages_outside_a_run(
            tmp_path / "run", RunParameters(bust_cache=True))


def test_for_workflow_test_run_grants_scope_but_read_only(tmp_path: Path) -> None:
    ctx = RunContext.for_workflow_test_run(tmp_path / "run", "proj", "r1")
    assert ctx.identity == RunIdentity(project="proj", run_id="r1")
    assert ctx.stage_cache is not None
    assert not hasattr(ctx.stage_cache, "record")
    assert ctx.params.queue_auto_approve is True  # safe: the read-only cache can't persist it


def test_for_stages_outside_a_run_allows_none_paths() -> None:
    ctx = RunContext.for_stages_outside_a_run(None)
    assert ctx.identity is None and ctx.stage_cache is None
    with pytest.raises(ValueError, match="no run_dir"):
        ctx.require_run_dir()
