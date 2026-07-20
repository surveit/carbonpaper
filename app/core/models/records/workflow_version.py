"""WorkflowVersion: one frozen snapshot of a project's authored artifacts —
its compiled stages (typed, embedded verbatim) and its schemas/ data model
(embedded raw) — taken at a point in time, plus who created it, why, its
parent, and the approval coverage AT creation time. Runs are pinned to a
version and read its embedded stages, so a run is reproducible against the
exact workflow it executed, never "whatever the working copy happened to be".

Defined here (not in app.services.versioning, which owns the create/list/load
operations) so it can embed the pure Stage contract directly; see
app.core.models.records for why a record — unlike the pure contracts
alongside app.core.models — may import PersistedModel."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.models.stage import Stage
from app.core.persistence import PersistedModel


class WorkflowVersion(PersistedModel):
    """One frozen snapshot, stored in the "workflow_version" collection. `id`
    (inherited from PersistedModel) is the composite `f"{project}/{version_id}"`;
    `version_id` is the plain local id every caller of app.services.versioning's
    four public functions works with. `stages` and `schemas` are the frozen
    artifacts; `coverage` is approval coverage computed against `stages` at
    creation time."""

    collection: ClassVar[str] = "workflow_version"
    # Dump the embedded stages in their canonical spec-dict shape (field aliases
    # restored, unset optionals dropped) — the same convention stage_to_spec_dict
    # uses, so a version's on-disk stage shape matches the working copy's.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    version_id: str
    created_at: str
    parent_version: str | None = None
    message: str
    reviewer: str
    coverage: dict[str, Any] = Field(default_factory=dict)
    stages: list[Stage] = Field(default_factory=list)
    schemas: list[dict[str, Any]] = Field(default_factory=list)
