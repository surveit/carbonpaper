"""The frozen run context.

Built once and threaded read-only; a run's growing state lives on the manifest.
A cache-WRITING context may not carry `queue_auto_approve`, and
`identity`/`stage_cache` co-vary (both or neither) - both enforced by validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel, ConfigDict, model_validator

from app.core.stage_cache import ReadOnlyStageCache, StageCache, StageCacheEntry

from .run_log import RunLog
from .run_parameters import RunParameters


@dataclass(frozen=True)
class RunIdentity:
    """A run's logical identity: the (project, run_id) pair cancellation's
    checkpoints poll (`app.runtime.cancellation`) and the stage-result cache key
    scopes to. Carried on `RunContext.identity`; absent (`None`) when stages
    execute outside a run — a preview, an authored stage test, an eval."""

    project: str
    run_id: str


class RunContext(BaseModel):
    """Immutable identity + config for one execution, built by exactly one of
    three constructors named for what is being executed: `for_workflow_run` (a
    workflow run — project scope, read+write cache), `for_workflow_test_run` (a
    workflow test's run — project scope, READ-ONLY cache), or
    `for_stages_outside_a_run` (a preview / authored stage test / eval — no
    scope, no cache, possibly no paths)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # A run's on-disk roots. Both are None only when stages execute outside any
    # run (the stage-test runner) — the stage types it runs read neither. Every
    # handler that DOES reach for run-scoped disk goes through require_run_dir()
    # and fails loudly on None rather than touching a fabricated directory;
    # repo_root has no reader in the runtime.
    repo_root: Path | None
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
    # What the caller asked of this run (row windows, cache busting, queue bypass,
    # bindings) — the same object the manifest records, so the settings executed
    # under and the settings written down cannot drift.
    params: RunParameters = RunParameters()
    # This run's event log (runs/<id>/events.jsonl), attached by the executor for
    # the duration of the run — see `attach_run_log`. Write-only from here: a
    # handler emits onto it and never reads it back, so it carries no run state
    # and nothing here becomes load-bearing. None outside a logged execution
    # (every emit site treats that as "don't log"), never a fabricated sink.
    run_log: RunLog | None = None

    @model_validator(mode="after")
    def _a_writable_cache_forbids_queue_auto_approve(self) -> RunContext:
        # The real hazard the old mode="production" guard was standing in for: an
        # auto-approved queue decides in memory, and a WRITE-capable cache would
        # persist stage results reached that way for a later workflow run to read
        # back as if a human had approved them. Read-only scope (a workflow test)
        # cannot, so it may bypass freely.
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
        """Skipping reads only makes sense where the run re-pins what it skipped."""
        if self.params.bust_cache and not isinstance(self.stage_cache, StageCache):
            raise ValueError(
                "bust_cache is set on a run whose stage cache is not writable — a "
                "run that cannot record what it recomputes would leave the cache "
                "exactly as stale as it found it, having paid to skip it."
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
        # The only copy-with-change a context allows: the log's lifetime is the run's, so
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
    def for_workflow_run(
        cls,
        repo_root: Path,
        run_dir: Path,
        project: str,
        run_id: str,
        params: RunParameters = RunParameters(),
    ) -> RunContext:
        """A workflow run's context (app.runtime.runner): full project scope —
        `identity` (`project`, `run_id`) and a read+WRITE stage-result cache
        (`StageCacheEntry.read_write()`). The run's growing telemetry lives on
        the manifest, not here, so a resume replays nothing through this
        constructor.

        `params.bust_cache` makes this run skip every cache READ; the accessor
        stays write-capable, so the run leaves the cache re-pinned rather than
        stale. Only a workflow run can be told this — it is the only kind that
        WRITES the cache, so the only kind whose skipped reads get re-pinned."""
        return cls(
            repo_root=repo_root,
            run_dir=run_dir,
            identity=RunIdentity(project=project, run_id=run_id),
            stage_cache=StageCacheEntry.read_write(),
            params=params,
        )

    @classmethod
    def for_workflow_test_run(
        cls,
        repo_root: Path,
        run_dir: Path,
        project: str,
        run_id: str,
        params: RunParameters = RunParameters(),
    ) -> RunContext:
        """A workflow test's run (app.services.workflow_test): the same project
        scope a workflow run gets — `identity` plus a stage-result cache — except
        the cache is READ-ONLY (`StageCacheEntry.read_only()`). So a publish
        stage's `trace_links` resolves and a slow upstream stage can replay a
        workflow run's cached result, but the view carries no `record` method:
        this run structurally cannot write a cache entry, and a test's outputs
        never poison what a workflow run reads back.

        The constructor named for a workflow test MAKES the run one: it stamps
        `is_test_run` and `queue_auto_approve` onto whatever params it is given, so
        a caller cannot ask for this context and record the run as a production
        one. The in-memory approvals are safe precisely because the read-only cache
        cannot persist them for a later run to mistake for human ones."""
        return cls(
            repo_root=repo_root,
            run_dir=run_dir,
            identity=RunIdentity(project=project, run_id=run_id),
            stage_cache=StageCacheEntry.read_only(),
            params=params.model_copy(
                update={"is_test_run": True, "queue_auto_approve": True}),
        )

    @classmethod
    def for_stages_outside_a_run(
        cls,
        repo_root: Path | None,
        run_dir: Path | None,
        params: RunParameters = RunParameters(),
        queue_auto_approve: bool = False,
    ) -> RunContext:
        """Stage handlers executed with no run behind them: a single-stage preview
        (app.runtime.preview), an authored stage test (app.runtime.stage_tests),
        an eval, a bare subset run. No `identity` and no stage-result cache, so a
        handler that needs project scope fails loudly rather than reading a
        fabricated wrong directory.

        `repo_root`/`run_dir` are both None when there is no run on disk at all —
        require_run_dir() then fails loudly for any stage that writes run-scoped
        output. `queue_auto_approve` lets a human_review_queue stage pass rows
        through in memory; there is no cache here to persist those approvals into,
        and nothing outside a run is a production run, so it carries `is_test_run`
        with it rather than tripping that invariant."""
        bypassing = queue_auto_approve or params.queue_auto_approve
        return cls(
            repo_root=repo_root,
            run_dir=run_dir,
            identity=None,
            stage_cache=None,
            params=params.model_copy(update={
                "queue_auto_approve": bypassing,
                "is_test_run": params.is_test_run or bypassing,
            }),
        )
