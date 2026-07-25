"""RunContext: the frozen, typed run context every stage handler receives
instead of the old `ctx: dict[str, Any]`. Pins its structural facts: no field
names a project directory; identity/stage_cache are granted (or withheld)
together; `mode` is stamped once and a product run may never carry the
in-memory queue bypass; and the run's telemetry lives on the manifest, not
here (no accumulator field to mutate)."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.runtime.context import RunContext, RunIdentity
from app.core.stage_cache import StageCacheEntry


def _make(**overrides: object) -> RunContext:
    identity = overrides.get("identity")
    defaults: dict[str, object] = dict(
        mode="product" if identity is not None else "non_product",
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


def test_product_run_context_rejects_queue_auto_approve(tmp_path: Path) -> None:
    """A product run must never carry the in-memory queue bypass: pairing
    mode='product' with queue_auto_approve fails loudly at construction."""
    with pytest.raises(ValidationError, match="non-product-run bypass"):
        RunContext(
            mode="product",
            repo_root=tmp_path,
            run_dir=tmp_path / "run",
            queue_auto_approve=True,
        )


def test_non_product_run_context_allows_queue_auto_approve(tmp_path: Path) -> None:
    """A non-product run may set the bypass — that is the only mode that can."""
    ctx = RunContext(
        mode="non_product",
        repo_root=tmp_path,
        run_dir=tmp_path / "run",
        queue_auto_approve=True,
    )
    assert ctx.queue_auto_approve is True


def test_run_context_is_frozen(tmp_path: Path) -> None:
    """Identity/config is immutable once built — nothing mutates it mid-run."""
    ctx = _make()
    with pytest.raises(ValidationError):
        ctx.run_dir = tmp_path / "other"  # type: ignore[misc]


def test_for_product_run_stamps_mode_and_grants_scope(tmp_path: Path) -> None:
    ctx = RunContext.for_product_run(tmp_path, tmp_path / "run", "proj", "r1")
    assert ctx.mode == "product"
    assert ctx.identity == RunIdentity(project="proj", run_id="r1")
    assert ctx.stage_cache is not None


def test_for_non_product_run_allows_none_paths() -> None:
    """The in-memory harness context carries no on-disk roots; require_run_dir
    fails loudly rather than handing back a fabricated path."""
    ctx = RunContext.for_non_product_run(None, None)
    assert ctx.mode == "non_product"
    assert ctx.identity is None and ctx.stage_cache is None
    with pytest.raises(ValueError, match="no run_dir"):
        ctx.require_run_dir()
