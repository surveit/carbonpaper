"""WorkflowVersion: one frozen snapshot of a project's authored artifacts —
its compiled stages (typed, embedded verbatim) and its schemas/ data model
(embedded raw) — taken at a point in time, plus who created it, why, its
parent, and the approval coverage AT creation time. Runs are pinned to a
version and read its embedded stages, so a run is reproducible against the
exact workflow it executed, never "whatever the working copy happened to be".

A version is born UNPUBLISHED (`published=False`); `published`/`published_at`/
`published_by` record the approval act (app.services.versioning.publish_version)
that makes a version eligible to run. See app.services.versioning for the
create/publish/list/load operations — this module defines only the stored
shape, so it can embed the pure Stage/Coverage contracts directly; see
app.core.models.records for why a record — unlike the pure contracts
alongside app.core.models — may import PersistedModel."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.models import Coverage, Stage
from app.core.persistence import PersistedModel


def _no_coverage() -> Coverage:
    # The zero-stage shape coverage_for itself returns for an empty stage list —
    # a WorkflowVersion constructed without an explicit `coverage=` (every
    # in-repo case is a test seeding a version directly) is born carrying it.
    return Coverage(approved=0, rejected=0, edited_stale=0, unreviewed=0,
                     total=0, approved_pct=0.0)


class WorkflowVersion(PersistedModel):
    """One frozen snapshot, stored in the "workflow_version" collection. `id` (inherited
    from PersistedModel) is the composite `f"{project}/{version_id}"`; `version_id`
    is the plain local id every caller of app.services.versioning's public
    functions works with. `stages` and `schemas` are the frozen artifacts;
    `coverage` is approval coverage computed against `stages` at creation
    time. `published` (plus `published_at`/`published_by`) records the
    approval act that makes a version runnable."""

    collection: ClassVar[str] = "workflow_version"
    # Dump the embedded stages in their canonical spec-dict shape (field aliases
    # restored, unset optionals dropped) — the same convention stage_to_spec_dict
    # uses, so a version's on-disk stage shape matches the working copy's.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    version_id: str
    parent_version: str | None = None
    message: str
    reviewer: str
    coverage: Coverage = Field(default_factory=_no_coverage)
    stages: list[Stage] = Field(default_factory=list)
    schemas: list[dict[str, Any]] = Field(default_factory=list)
    published: bool = False
    published_at: str | None = None
    published_by: str | None = None
