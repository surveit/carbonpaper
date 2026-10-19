from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.record import PersistedModel, PersistenceScope
from app.models.row_types import RowType
from app.models.schema import _Base
from app.models.stage import STAGE_SPEC_SCHEMA_VERSION, Stage
from app.models.terms import Verb


MAX_MESSAGE_CHARS = 150


class Method(_Base):
    """The project's row types, verbs and methodology as they stood when the version was saved."""

    row_types: list[RowType]
    verbs: list[Verb]
    # None: the project had no methodology.
    methodology: str | None = None


class WorkflowVersion(PersistedModel):
    """`id` is the composite `{project_id}/{version_id}`."""

    collection: ClassVar[str] = "workflow_version"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    SCHEMA_VERSION: ClassVar[int] = STAGE_SPEC_SCHEMA_VERSION
    # The spec-dict shape: field aliases restored, unset optionals dropped.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    version_id: str
    parent_version: str | None = None
    message: str = Field(
        description=f"What identifies this version to a reader in {MAX_MESSAGE_CHARS} "
                    f"characters or fewer."
    )
    stages: list[Stage] = Field(default_factory=list)
    schemas: list[dict[str, Any]] = Field(default_factory=list)
    # None: saved before a version kept the project's method.
    method: Method | None = None
