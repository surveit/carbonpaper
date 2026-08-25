from __future__ import annotations

from typing import ClassVar

from app.core.record import PersistedModel, PersistenceScope


class Project(PersistedModel):
    """`authored_at` is the project's own date; `created_at` stamps when this RECORD was written."""

    collection: ClassVar[str] = "project"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    # Must stay optional: a project older than labels has no `name`, and would orphan.
    name: str | None = None
    title: str | None = None
    model: str | None = None
    source: str | None = None
    authored_at: str | None = None
    # Hidden from every listing; still reachable at its own URL. A stand-in until permissions.
    private: bool = False

    def label(self) -> str:
        return self.name or self.id

    def display_name(self) -> str:
        """What every surface SHOWS. `label` stays the slug callers look a project up by."""
        return self.title or self.label()
