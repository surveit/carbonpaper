"""Draft: one disposable, mutable scratch document in the "draft" collection —
where an agent (or, later, a UI edit buffer) assembles a workflow's stages
before freezing them into an immutable WorkflowVersion. Unlike a version it
carries no promise of survival: anything may delete a draft at any time,
nothing may depend on one existing, and a draft is never project state (not
versioned, not run). See app.services.drafts for the create/read/edit/save
operations; this module defines only the stored shape, so it can embed the
pure Stage contract directly; see app.core.models.records for why a record —
unlike the pure contracts alongside app.core.models — may import
PersistedModel."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.models import Stage
from app.core.persistence import PersistedModel


class Draft(PersistedModel):
    """One scratch document in the "draft" collection. `id` (inherited from
    PersistedModel) is the composite `f"{project}/{draft_id}"`; `draft_id` is
    the plain local id every caller of app.services.drafts's public functions
    works with. `stages` are validated `Stage` objects — each one individually
    valid — but the WORKFLOW they form may still be incomplete mid-edit (a
    dangling input, a duplicate id, a cycle) until save_version."""

    collection: ClassVar[str] = "draft"
    # Dump embedded stages in their canonical spec-dict shape (field aliases
    # restored, unset optionals dropped) — mirrors WorkflowVersion.DUMP_OPTS,
    # so a draft's on-disk stage shape matches a version's.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    draft_id: str
    parent_version: str | None = None
    stages: list[Stage] = Field(default_factory=list)
