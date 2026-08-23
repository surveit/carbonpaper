from __future__ import annotations

from typing import ClassVar

from app.core.record import PersistedModel, PersistenceScope


class RunManifestMetadata(PersistedModel):
    collection: ClassVar[str] = "run_manifest_metadata"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project_id: str
    run_id: str
    archived: bool = False
    # Empty is unnamed; clearing returns it there.
    name: str = ""
