"""WorkflowRun: one run's manifest — the runner's in-progress/finished record
for one execution of a project's pinned workflow version.

Defined here (not in app.services.run_store, which owns the save/load/list
operations) so it sits alongside the other records; see
app.core.models.records for why a record — unlike the pure contracts
alongside app.core.models — may import PersistedModel."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.persistence import PersistedModel


class WorkflowRun(PersistedModel):
    """One run's manifest, stored in the "workflow_run" collection. `id`
    (inherited from PersistedModel) is the composite `f"{project}/{run_id}"`;
    `run_id` is the plain local id every caller of app.services.run_store's
    functions works with. `stages` is the per-stage execution log: kept as
    dicts (mutation-heavy, rewritten whole on every stage completion by
    `app.runtime.runner._execute_stages`) rather than typed — not worth full
    typing for a record that is never read field-by-field here."""

    collection: ClassVar[str] = "workflow_run"

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
