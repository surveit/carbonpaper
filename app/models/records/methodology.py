from __future__ import annotations

from typing import ClassVar

from app.core.persistence import PersistedModel, PersistenceScope


class Methodology(PersistedModel):
    """One project's authored prose, `id`'d by project name."""

    collection: ClassVar[str] = "methodology"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    text: str
