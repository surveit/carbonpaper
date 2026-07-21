"""WorkflowRun: one run's manifest — the runner's in-progress/finished record
for one execution of a project's pinned workflow version.

`WorkflowRun` is its own storage API (Active-Record): `.save()` / `.load(id)`
(both from PersistedModel) / `.list_for_project(project)` below — there is no
separate service module wrapping it the way app.services.versioning wraps
WorkflowVersion. See app.core.models.records for why a record — unlike the
pure contracts alongside app.core.models — may import PersistedModel."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.persistence import PersistedModel

# Shared by the sub-models below (not WorkflowRun itself, which inherits
# PersistedModel's copy). Mirrors PersistedModel's / app.core.models.schema._Base's
# ConfigDict verbatim rather than importing either: unknown keys are rejected —
# a typo'd field on a stage record is invalid data, not silently-dropped data —
# and this module's only up-the-stack dependency stays the storage base.
_STRICT = ConfigDict(extra="forbid", use_enum_values=True, validate_default=True, populate_by_name=True)


class ValidationIssue(BaseModel):
    """One schema-check finding — the typed shape of
    `app.runtime.validation.Issue` — embedded in a ValidationReport."""

    model_config = _STRICT

    severity: str  # "error" | "warning"
    column: str | None
    message: str


class ValidationReport(BaseModel):
    """A stage input or output's schema-check result — the typed shape of
    what `app.runtime.validation.validate_dataframe` returns (its
    `.to_dict()`), embedded on a StageRun. `phase` is `"input:<upstream-stage-
    id>"` for an input check or `"output"` for the stage's own output check."""

    model_config = _STRICT

    stage_id: str
    phase: str
    rows: int = 0
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class StageError(BaseModel):
    """A stage's failure, recorded on its StageRun when the handler raises or
    a row fails generation."""

    model_config = _STRICT

    type: str
    message: str
    traceback: str | None = None


class StageRun(BaseModel):
    """One stage's execution record within a WorkflowRun. Only `stage_id`/
    `type`/`name` are required — every other field defaults, so a bare
    `StageRun(stage_id=..., type=..., name=...)` is a valid "pending" stub,
    the shape the runner seeds every not-yet-started stage with."""

    model_config = _STRICT

    stage_id: str
    type: str
    name: str
    status: str = "pending"
    input_validation: list[ValidationReport] = Field(default_factory=list)
    output_validation: ValidationReport | None = None
    elapsed_ms: int = 0
    rows: int = 0
    error: StageError | None = None
    started_at: str | None = None
    finished_at: str | None = None
    notes: list[str] = Field(default_factory=list)
    queue_path: str | None = None
    output_path: str | None = None


class WorkflowRun(PersistedModel):
    """One run's manifest, stored in the "workflow_run" collection. `id`
    (inherited from PersistedModel, along with `created_at`/`updated_at` —
    both auto-stamped by PersistedModel.save(), so this record does not
    redeclare either) is the composite `f"{project}/{run_id}"`; `run_id` is
    the plain local id every caller works with. `stages` is the per-stage
    execution log — StageRun records, rewritten whole on every stage
    completion by `app.runtime.runner._execute_stages`. `project` is None for
    an ephemeral subset/eval run (app.runtime.runner.run_subset): that
    manifest is keyed on a Workflow + run_dir rather than a project tree, and
    is never saved."""

    collection: ClassVar[str] = "workflow_run"

    run_id: str
    project: str | None = None
    status: str
    workflow_version: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    resumed_at: str | None = None
    halted_at: str | None = None
    limit_overrides: dict[str, int] = Field(default_factory=dict)
    offset_overrides: dict[str, int] = Field(default_factory=dict)
    # Connector params are a genuinely open, per-connector-kind bag — the same
    # honest JSON boundary as PersistedModel's JsonDict, not a typing dodge.
    run_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Stage-handler-owned bookkeeping (only human_review_queue writes it, keyed
    # by stage id); its shape isn't a contract this record enforces, so it
    # stays the same honest JSON boundary as run_bindings/input_bindings above.
    queue_stats: dict[str, Any] = Field(default_factory=dict)
    dropped_columns: dict[str, list[str]] = Field(default_factory=dict)
    stages: list[StageRun] = Field(default_factory=list)

    @classmethod
    def list_for_project(cls, project: str) -> list[WorkflowRun]:
        """Every run for `project`, NEWEST-FIRST (run ids are strftime
        timestamps, so a reverse id sort is chronological — the same trick
        app.services.versioning uses for version ids). A stored run document
        that fails this model's contract raises ValidationError rather than
        being silently dropped (mirrors app.evals.store.list_eval_runs: one
        malformed document fails the whole listing instead of presenting the
        store as healthy). No runs stored yet -> []."""
        runs = cls.list(prefix=f"{project}/")
        runs.sort(key=lambda r: r.run_id, reverse=True)
        return runs
