"""The run a project stands behind: its figures are the project's numbers."""
from __future__ import annotations

from typing import ClassVar

from app.core.record import PersistedModel, PersistenceScope


class PublishedRun(PersistedModel):
    collection: ClassVar[str] = "published_run"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project_id: str
    run_id: str
