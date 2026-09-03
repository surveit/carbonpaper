"""The frozen run context.

Built once and threaded read-only; a run's growing state lives on the manifest.
A cache-WRITING context may not carry `queue_auto_approve`, and
`identity`/`stage_cache` co-vary (both or neither) - both enforced by validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.stage_cache import ReadOnlyStageCache, StageCache, StageCacheEntry
from app.models.review_ledger import ReviewLedger

from .run_log import RunLog
from .progress import StageProgressReporter
from app.models.run_parameters import RunParameters


@dataclass(frozen=True)
class RunIdentity:
    project: str
    run_id: str


class RunContext(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # A run's on-disk root. None only when stages execute outside any run (the
    # stage-test runner) — the stage types it runs read it never. Every handler
    # that DOES reach for run-scoped disk goes through require_run_dir() and
    # fails loudly on None rather than touching a fabricated directory.
    run_dir: Path | None
    # This run's logical identity, read by cancellation's checkpoints and the
    # stage-result cache key. Set for a workflow run and a workflow test's run;
    # None outside a run, which is therefore simply not cancellable and carries
    # no cache scope.
    identity: RunIdentity | None = None
    # The stage-result cache view this run may read (and, for a workflow run,
    # write via the writable `StageCache` subclass). None alongside
    # `identity is None` — enforced by the validator.
    stage_cache: ReadOnlyStageCache | None = None
    # A decided row's source of truth, read before stage_cache. Covaries with identity.
    decisions: ReviewLedger | None = None
    # What the caller asked of this run (row windows, cache busting, queue bypass,
    # bindings) — the same object the manifest records, so the settings executed
    # under and the settings written down cannot drift.
    params: RunParameters = RunParameters()
    # This run's event log, attached by the executor for
    # the duration of the run — see `attach_run_log`. Write-only from here: a
    # handler emits onto it and never reads it back, so it carries no run state
    # and nothing here becomes load-bearing. None outside a logged execution
    # (every emit site treats that as "don't log"), never a fabricated sink.
    run_log: RunLog | None = None
    stage_progress: StageProgressReporter = Field(default_factory=StageProgressReporter)

    @model_validator(mode="after")
    def _a_writable_cache_forbids_queue_auto_approve(self) -> RunContext:
        if isinstance(self.stage_cache, StageCache) and self.params.queue_auto_approve:
            raise ValueError(
                "queue_auto_approve is set on a run whose stage cache is "
                "WRITABLE — its in-memory approvals would be recorded for a "
                "later run to read back as human decisions. Only a run with a "
                "read-only cache, or none at all, may bypass the queue."
            )
        return self

    @model_validator(mode="after")
    def _busting_requires_a_writable_cache(self) -> RunContext:
        if self.params.bust_cache and not isinstance(self.stage_cache, StageCache):
            raise ValueError(
                "bust_cache is set on a run whose stage cache is not writable — a "
                "run that cannot record what it recomputes would leave the cache "
                "exactly as stale as it found it, having paid to skip it."
            )
        return self

    @model_validator(mode="after")
    def _identity_and_cache_covary(self) -> RunContext:
        project_scoped = {self.identity is None, self.stage_cache is None, self.decisions is None}
        if len(project_scoped) != 1:
            raise ValueError(
                "RunContext.identity, RunContext.stage_cache and RunContext.decisions "
                "must all be set or all be None — a run either has project scope "
                "(all three) or it doesn't (none)"
            )
        return self

    def attach_run_log(self, log: RunLog) -> RunContext:
        return self.model_copy(update={"run_log": log})

    def attach_stage_progress(self, progress: StageProgressReporter) -> RunContext:
        return self.model_copy(update={"stage_progress": progress})

    def require_identity(self) -> RunIdentity:
        """This run's (project, run id), for a handler storing a run-scoped record."""
        if self.identity is None:
            raise ValueError(
                "this run context has no identity — it is an in-memory harness "
                "context, so a stage that stores a run-scoped record cannot "
                "execute under it."
            )
        return self.identity

    def require_run_dir(self) -> Path:
        if self.run_dir is None:
            raise ValueError(
                "this run context has no run_dir — it is an in-memory harness "
                "context (no run on disk), so a stage that writes run-scoped "
                "output cannot execute under it."
            )
        return self.run_dir

    @classmethod
    def for_workflow_run(
        cls,
        run_dir: Path,
        project_id: str,
        run_id: str,
        params: RunParameters = RunParameters(),
    ) -> RunContext:
        return cls(
            run_dir=run_dir,
            identity=RunIdentity(project=project_id, run_id=run_id),
            stage_cache=StageCacheEntry.read_write(),
            decisions=ReviewLedger(project_id),
            params=params,
        )

    @classmethod
    def for_workflow_test_run(
        cls,
        run_dir: Path,
        project_id: str,
        run_id: str,
        params: RunParameters = RunParameters(),
    ) -> RunContext:
        return cls(
            run_dir=run_dir,
            identity=RunIdentity(project=project_id, run_id=run_id),
            stage_cache=StageCacheEntry.read_only(),
            decisions=ReviewLedger(project_id),
            params=params.model_copy(
                update={"is_test_run": True, "queue_auto_approve": True}),
        )

    @classmethod
    def for_stages_outside_a_run(
        cls,
        run_dir: Path | None,
        params: RunParameters = RunParameters(),
        queue_auto_approve: bool = False,
    ) -> RunContext:
        bypassing = queue_auto_approve or params.queue_auto_approve
        return cls(
            run_dir=run_dir,
            identity=None,
            stage_cache=None,
            params=params.model_copy(update={
                "queue_auto_approve": bypassing,
                "is_test_run": params.is_test_run or bypassing,
            }),
        )
