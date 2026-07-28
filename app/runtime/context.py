"""The frozen run context.

Built once and threaded read-only; a run's growing state lives on the manifest.
A production-mode context may not carry `queue_auto_approve`, and
`identity`/`stage_cache` co-vary (both or neither) - both enforced by validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.stage_cache import ReadOnlyStageCache, StageCacheEntry

from .run_log import RunLog

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
    # Recompute everything: this run SKIPS every stage-cache read, while the
    # write-capable accessor still records what it computes — so the cache ends
    # the run re-pinned, not stale. Per-run only; nothing about a stage says it.
    bust_cache: bool = False
    # This run's event log (runs/<id>/events.jsonl), attached by the executor for
    # the duration of the run — see `attach_run_log`. Write-only from here: a
    # handler emits onto it and never reads it back, so it carries no run state
    # and nothing here becomes load-bearing. None outside a logged execution
    # (every emit site treats that as "don't log"), never a fabricated sink.
    run_log: RunLog | None = None

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
    def _busting_requires_a_cache(self) -> RunContext:
        if self.bust_cache and self.stage_cache is None:
            raise ValueError(
                "bust_cache is set on a run with no stage cache to bust — a run "
                "without project scope reads no cache in the first place, so "
                "asking it to skip those reads describes nothing."
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

    def attach_run_log(self, log: RunLog) -> RunContext:
        """A copy of this context carrying `log` — the executor's one attachment point."""
        # The only derivation of a context: the log's lifetime is the run's, so
        # it cannot be set by the constructors (which run before the run's
        # directory is being written to) without leaking a writer thread when a
        # prepared run is never executed.
        return self.model_copy(update={"run_log": log})

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
        limits: dict[str, int] | None = None,
        offsets: dict[str, int] | None = None,
        bust_cache: bool = False,
    ) -> RunContext:
        """A production run's context: `mode="production"`, full project scope —
        `identity` (`project`, `run_id`) and a read+write stage-result cache
        (`StageCacheEntry.read_write()`). The run's growing telemetry lives on
        the manifest, not here, so a resume replays nothing through this
        constructor.

        `bust_cache` makes this run skip every cache READ; the accessor stays
        write-capable, so the run leaves the cache re-pinned rather than stale.
        Only a production run can be told this — it is the only kind that has a
        cache — which is why `for_non_production_run` takes no such argument."""
        return cls(
            mode="production",
            repo_root=repo_root,
            run_dir=run_dir,
            identity=RunIdentity(project=project, run_id=run_id),
            stage_cache=StageCacheEntry.read_write(),
            limits=dict(limits or {}),
            offsets=dict(offsets or {}),
            bust_cache=bust_cache,
        )

    @classmethod
    def for_non_production_run(
        cls,
        repo_root: Path | None,
        run_dir: Path | None,
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
