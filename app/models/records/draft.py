from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.record import PersistedModel, PersistenceScope
from app.models.stage import STAGE_SPEC_SCHEMA_VERSION, Stage


class Draft(PersistedModel):
    """`id` is the composite `f"{project_id}/{draft_id}"`; `draft_id` is the local id callers use."""

    collection: ClassVar[str] = "draft"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    SCHEMA_VERSION: ClassVar[int] = STAGE_SPEC_SCHEMA_VERSION
    # The same spec-dict shape a version freezes its stages in.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    draft_id: str
    parent_version: str | None = None
    stages: list[Stage] = Field(default_factory=list)
