"""Run manifest storage: a project run's manifest as a document in the store's
"run" collection.

A run is a `Run` document — the runner's in-progress/finished manifest for one
execution of a project's pinned workflow version. Each document id is
`f"{project}/{run_id}"` — project-scoped, like every other collection in the
store — so listing or loading against a project with no runs yet returns empty
results rather than requiring any scaffolding to exist first. `run_id` uses the
same timestamp scheme as version ids (datetime.now().strftime('%Y%m%dT%H%M%S')),
so a reverse id sort is chronological.

Only the manifest lives here. Stage-output parquet, publish artifacts, and
human-review queue snapshots stay files under `<project>/runs/<run_id>/` —
frame-shaped payloads a FrameStore takes over in a later slice; this module
converts only the run's own bookkeeping record.

Placed in `app.services` (not `app.runtime`, where the runner that writes
through it lives) so `app.services.project` can read run summaries without
importing the runtime layer — the import-linter contract keeps
`app.services` runtime-free, the same reason `app.services.versioning` (not
`app.runtime`) holds workflow-version snapshots even though the runner reads
them too.

An ephemeral eval/preview subset run (`app.runtime.runner.run_subset`) never
reaches this module: its manifest is returned in-memory and never persisted
(see `runner._subset_ctx`'s no-op `persist_manifest`)."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.persistence import PersistedModel


class Run(PersistedModel):
    """One run's manifest, stored in the "run" collection. `id` (inherited from
    PersistedModel) is the composite `f"{project}/{run_id}"`; `run_id` is the
    plain local id every caller of this module's functions works with.
    `stages` is the per-stage execution log: kept as dicts (mutation-heavy,
    rewritten whole on every stage completion by
    `app.runtime.runner._execute_stages`) rather than typed — not worth full
    typing for a record that is never read field-by-field here."""

    collection: ClassVar[str] = "run"

    run_id: str
    project: str
    status: str
    workflow_version: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None
    resumed_at: str | None = None
    halted_at: str | None = None
    limit_overrides: dict[str, int] = Field(default_factory=dict)
    offset_overrides: dict[str, int] = Field(default_factory=dict)
    run_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    queue_stats: dict[str, Any] = Field(default_factory=dict)
    dropped_columns: dict[str, Any] = Field(default_factory=dict)
    stages: list[dict[str, Any]] = Field(default_factory=list)


def save_run(project: str, manifest: dict[str, Any]) -> None:
    """Save `manifest` as the run document `{project}/{manifest['run_id']}`.
    Called on every flush during execution (the initial write, each stage
    completion, the final write, and resume) — `Run`'s `extra="forbid"`
    config validates the manifest's shape on every call, so a malformed
    manifest (an unexpected or missing key — a runner bug) fails loudly
    rather than silently persisting the wrong shape."""
    Run.model_validate({"id": f"{project}/{manifest['run_id']}", **manifest}).save()


def load_run(project: str, run_id: str) -> dict[str, Any]:
    """The manifest dict for one run — the same shape callers expect
    (`run_id`, not the composite store id `project/run_id`). Raises
    `DocumentNotFound` if no such run is stored; each caller translates that
    to its own contract (`app.web.loading.load_manifest` -> HTTP 404,
    `app.runtime.runner.resume_run` -> `FileNotFoundError`,
    `app.runtime.trace` -> `FileNotFoundError`)."""
    run = Run.load(f"{project}/{run_id}")
    return run.model_dump(mode="json", exclude={"id"})


def list_runs(project: str) -> list[Run]:
    """Every run for a project, NEWEST-FIRST (run ids are strftime
    timestamps, so a reverse id sort is chronological — the same trick
    `app.services.versioning` uses for version ids). A stored run document
    that fails the `Run` contract raises `ValidationError` rather than being
    silently dropped (mirrors `app.evals.store.list_eval_runs`: one malformed
    document fails the whole listing instead of presenting the store as
    healthy). No runs stored yet -> []."""
    runs = Run.list(prefix=f"{project}/")
    runs.sort(key=lambda r: r.run_id, reverse=True)
    return runs


__all__ = ["Run", "save_run", "load_run", "list_runs"]
