from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.record import PersistedModel, PersistenceScope
from app.models.stage import STAGE_SPEC_SCHEMA_VERSION, Stage


MAX_MESSAGE_CHARS = 150


class WorkflowVersion(PersistedModel):
    """`id` is the composite `{project_id}/{version_id}`."""

    collection: ClassVar[str] = "workflow_version"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    SCHEMA_VERSION: ClassVar[int] = STAGE_SPEC_SCHEMA_VERSION
    # The spec-dict shape: field aliases restored, unset optionals dropped.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    version_id: str
    parent_version: str | None = None
    # Held when a version is written, not here: descriptions stored before it still load.
    message: str
    stages: list[Stage] = Field(default_factory=list)
    schemas: list[dict[str, Any]] = Field(default_factory=list)
