"""The frozen run context.

`RunContext` is the immutable identity + config a run is executed under. It is
built once — by `RunContext.for_production_run` (a production run:
`app.runtime.runner.prepare_run`/`resume_run`), by `RunContext.for_non_production_run`
(a subset run, a preview, an authored-test run) — and threaded read-only through
the executor, the row driver, and every stage handler. Nothing mutates it
mid-run: the run's growing state (per-stage token usage, dropped-column notes,
queue stats, row-generation errors) lives on the manifest, not here — a handler
reports it back as a `StageContribution` (app.runtime.manifest).

`mode` is stamped at construction and never changes: a production run cannot
carry `queue_auto_approve` (the in-memory queue bypass evals/workflow-test use),
so a context that pairs the two fails loudly here rather than silently
auto-approving a production run's review queue.

`identity`/`stage_cache` are the pair a caller chooses at construction:
`for_production_run` grants both (this run's (project, run_id) and a read+write
stage-result cache); `for_non_production_run` grants neither. They co-vary by
construction — there is no state with cache access but no identity, or the
reverse — enforced by the validator so a hand-built context can't violate it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.stage_cache import ReadOnlyStageCache, StageCacheEntry
from app.models import Stage

RunMode = Literal["production", "non_production"]


@dataclass(frozen=True)
class RunIdentity:
    """A production run's logical identity: the (project, run_id) pair
    cancellation's checkpoints poll (`app.runtime.cancellation`) and the
    stage-result cache key scopes to. Carried on `RunContext.identity`; absent
    (`None`) for a run with no project scope — a subset run or an in-memory
    preview."""

    project: str
    run_id: str


class RunContext(BaseModel):
    """Immutable identity + config for one run. `for_production_run` sets
    `mode="production"` and grants project scope (`identity` + a read+write
    stage-result cache); `for_non_production_run` sets `mode="non_production"`, may
    set `queue_auto_approve`, and grants no project scope."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    mode: RunMode
    # A real run's on-disk roots. Both are None only for an in-memory harness
    # that executes a handler outside any run (the stage-test runner) — the stage
    # types it runs read neither. Every handler that DOES reach for run-scoped
    # disk goes through require_run_dir() and fails loudly on None rather than
    # touching a fabricated directory; repo_root has no reader in the runtime.
    repo_root: Path | None
    run_dir: Path | None
    # The stages this execution is running, keyed by id — the workflow as pinned
    # when the context was built. A handler still receives its OWN stage as an
    # argument; this is here for the publish handler's trace exporter, which
    # renders every UPSTREAM stage's transform onto an exported trace page and
    # has no other way to see them.
    stages: dict[str, Stage]
    # This run's logical identity, read by cancellation's checkpoints and the
    # stage-result cache key. Set for a production run; None for a subset run,
    # which is therefore simply not cancellable and carries no cache scope.
    identity: RunIdentity | None = None
    # The stage-result cache view this run may read (and, for a production run,
    # write via the writable `StageCache` subclass). None alongside
    # `identity is None` — enforced by the validator.
    stage_cache: ReadOnlyStageCache | None = None
    limits: dict[str, int] = {}
    offsets: dict[str, int] = {}
    # In-memory queue bypass: when set, a human_review_queue stage approves every
    # row in memory instead of reaching for the stage cache or halting. Only a
    # non-production run may set it (see the validator).
    queue_auto_approve: bool = False

    @model_validator(mode="after")
    def _production_run_forbids_queue_auto_approve(self) -> RunContext:
        if self.mode == "production" and self.queue_auto_approve:
            raise ValueError(
                "queue_auto_approve is a non-production-run bypass; a production run "
                "must never auto-approve its human review queue in memory. Build "
                "the context with mode='non_production' if the bypass is intended."
            )
        return self

    @model_validator(mode="after")
    def _identity_and_cache_covary(self) -> RunContext:
        if (self.identity is None) != (self.stage_cache is None):
            raise ValueError(
                "RunContext.identity and RunContext.stage_cache must both be "
                "set or both be None — a run either has project scope (both) "
                "or it doesn't (neither)"
            )
        return self

    def require_run_dir(self) -> Path:
        """This run's on-disk dir, for a handler that writes run-scoped output
        (publish artifacts, the queue snapshot). Fails loudly on the harness
        context that carries none, so a stage reaching for run disk outside a
        run is a clear error, not a write to a fabricated path."""
        if self.run_dir is None:
            raise ValueError(
                "this run context has no run_dir — it is an in-memory harness "
                "context (no run on disk), so a stage that writes run-scoped "
                "output cannot execute under it."
            )
        return self.run_dir

    @classmethod
    def for_production_run(
        cls,
        repo_root: Path,
        run_dir: Path,
        project: str,
        run_id: str,
        stages: list[Stage],
        limits: dict[str, int] | None = None,
        offsets: dict[str, int] | None = None,
    ) -> RunContext:
        """A production run's context: `mode="production"`, full project scope —
        `identity` (`project`, `run_id`) and a read+write stage-result cache
        (`StageCacheEntry.read_write()`). The run's growing telemetry lives on
        the manifest, not here, so a resume replays nothing through this
        constructor."""
        return cls(
            mode="production",
            repo_root=repo_root,
            run_dir=run_dir,
            stages={stage.id: stage for stage in stages},
            identity=RunIdentity(project=project, run_id=run_id),
            stage_cache=StageCacheEntry.read_write(),
            limits=dict(limits or {}),
            offsets=dict(offsets or {}),
        )

    @classmethod
    def for_non_production_run(
        cls,
        repo_root: Path | None,
        run_dir: Path | None,
        stages: list[Stage],
        limits: dict[str, int] | None = None,
        offsets: dict[str, int] | None = None,
        queue_auto_approve: bool = False,
    ) -> RunContext:
        """A run with no project scope (a subset run, a preview, an authored-test
        run): `mode="non_production"`, no `identity`, no stage-result cache.
        `repo_root`/`run_dir` are None for an in-memory harness that executes a
        handler outside any run. `queue_auto_approve` lets such a run pass a
        human_review_queue stage through in memory (it carries no project scope
        to resolve cached decisions against)."""
        return cls(
            mode="non_production",
            repo_root=repo_root,
            run_dir=run_dir,
            stages={stage.id: stage for stage in stages},
            identity=None,
            stage_cache=None,
            limits=dict(limits or {}),
            offsets=dict(offsets or {}),
            queue_auto_approve=queue_auto_approve,
        )


__all__ = [
    "RunMode",
    "RunIdentity",
    "RunContext",
]
