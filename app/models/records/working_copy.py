from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from app.core.json_types import JsonDict
from app.core.record import PersistedModel, PersistenceScope
from app.models.stage import STAGE_SPEC_SCHEMA_VERSION, Stage


class WorkingCopy(PersistedModel):
    """A project's mutable stage list, `id`'d by project name."""

    collection: ClassVar[str] = "working_copy"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    SCHEMA_VERSION: ClassVar[int] = STAGE_SPEC_SCHEMA_VERSION
    # The same spec-dict shape a version freezes its stages in.
    DUMP_OPTS: ClassVar[JsonDict] = {"by_alias": True, "exclude_none": True}

    stages: list[Stage] = Field(default_factory=list)
