"""The run context: everything a stage handler is handed alongside its inputs.

`RunContext` replaces the untyped `ctx: dict[str, Any]` every handler used to
receive. Its fields are exactly today's ctx keys minus `project_dir` — a
handler cannot reach a project's on-disk directory through `RunContext`,
because nothing here names one. `identity`/`stage_cache` are the one pair of
fields a caller chooses at construction time: `RunContext.for_production`
grants both (a production run — `app.runtime.runner.prepare_run`/`resume_run`);
`RunContext.for_non_production` grants neither (a subset run, a preview, an
authored-test run). The two co-vary by construction (see
`RunContext.__post_init__`) — there is no state where a run has cache access
but no identity, or the reverse.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from app.core.agent.usage import LlmUsage
from app.services.stage_cache import CacheMode, ReadOnlyStageCache, StageCacheEntry

from .llm import LlmBackendStatus


@dataclass(frozen=True)
class RunIdentity:
    """A production run's logical identity: the (project, run_id) pair
    cancellation's checkpoints poll (`app.runtime.cancellation`) and a future
    cache key would scope to. Carried on `RunContext.identity`; absent
    (`None`) for a run with no project scope — a subset run or an in-memory
    preview."""

    project: str
    run_id: str


class QueueStats(TypedDict):
    """One human_review_queue stage's item counts for one run, as recorded on
    `RunContext.queue_stats` and read back into the run manifest."""

    items_queued_total: int
    items_passed_through: int
    items_pending: int
    items_decided: int


class RowError(TypedDict):
    """One row a stage's per-row function failed to produce: its 0-based
    position in the stage's output and the failure message, as recorded on
    `RunContext.row_errors`."""

    row: int
    message: str


@dataclass
class RunContext:
    """Everything a stage handler is handed alongside its typed inputs.

    Grants (chosen once, at the entry point that builds this context):
      repo_root, run_dir — where this run reads/writes on disk.
      identity           — this run's (project, run_id), or None for a run
                            with no project scope.
      stage_cache        — the stage-result cache view this run may read
                            (and, for a production run, write via the
                            writable `StageCache` subclass); None alongside
                            `identity is None`.

    Run input overrides:
      limits, offsets — per-stage row-slicing overrides for this run.
      queue_auto_approve — when set, a human_review_queue stage passes every
                           row through as approved, in memory (no stage-cache
                           decision lookup, no queue snapshot, no halt). Only a
                           non-production run ever sets it; production leaves it
                           False so the real review path is unchanged.

    Telemetry accumulators (stages write into these; the manifest reads
    them back): queue_stats, dropped_columns, row_errors, llm_usage,
    llm_backend — one dict per existing ctx key, keyed by stage id.
    """

    repo_root: Path
    run_dir: Path
    identity: RunIdentity | None
    stage_cache: ReadOnlyStageCache | None
    limits: dict[str, int]
    offsets: dict[str, int]
    queue_auto_approve: bool = False
    queue_stats: dict[str, QueueStats] = field(default_factory=dict)
    dropped_columns: dict[str, list[str]] = field(default_factory=dict)
    row_errors: dict[str, list[RowError]] = field(default_factory=dict)
    llm_usage: dict[str, LlmUsage] = field(default_factory=dict)
    llm_backend: dict[str, LlmBackendStatus] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.identity is None) != (self.stage_cache is None):
            raise ValueError(
                "RunContext.identity and RunContext.stage_cache must both be "
                "set or both be None — a run either has project scope (both) "
                "or it doesn't (neither)"
            )

    @classmethod
    def for_production(
        cls,
        repo_root: Path,
        run_dir: Path,
        project: str,
        run_id: str,
        limits: dict[str, int] | None = None,
        offsets: dict[str, int] | None = None,
        queue_stats: dict[str, QueueStats] | None = None,
        dropped_columns: dict[str, list[str]] | None = None,
    ) -> "RunContext":
        """A production run's context: full project scope — `identity`
        (`project`, `run_id`) and a read+write stage-result cache
        (`StageCacheEntry.for_mode(CacheMode.PRODUCTION)`). `queue_stats`/
        `dropped_columns` default to fresh dicts (a new run); a resumed run
        passes in the prior run's values so telemetry survives the resume."""
        return cls(
            repo_root=repo_root, run_dir=run_dir,
            identity=RunIdentity(project=project, run_id=run_id),
            stage_cache=StageCacheEntry.for_mode(CacheMode.PRODUCTION),
            limits=dict(limits or {}), offsets=dict(offsets or {}),
            queue_stats=dict(queue_stats or {}), dropped_columns=dict(dropped_columns or {}),
        )

    @classmethod
    def for_non_production(
        cls,
        repo_root: Path,
        run_dir: Path,
        limits: dict[str, int] | None = None,
        offsets: dict[str, int] | None = None,
        queue_auto_approve: bool = False,
    ) -> "RunContext":
        """A run with no project scope (a subset run, a preview, an authored-
        test run): no `identity`, no stage-result cache. `queue_auto_approve`
        lets such a run pass a human_review_queue stage through in memory
        (it carries no project scope to resolve cached decisions against)."""
        return cls(
            repo_root=repo_root, run_dir=run_dir, identity=None, stage_cache=None,
            limits=dict(limits or {}), offsets=dict(offsets or {}),
            queue_auto_approve=queue_auto_approve,
        )


__all__ = ["RunIdentity", "RunContext", "QueueStats", "RowError"]
