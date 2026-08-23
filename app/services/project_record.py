"""The project record, and the name to show for an id."""

from __future__ import annotations

from typing import ClassVar

from app.core.persistence import PersistedModel, PersistenceScope


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

    def label(self) -> str:
        return self.name or self.id

    def display_name(self) -> str:
        """What every surface SHOWS. `label` stays the slug callers look a project up by."""
        return self.title or self.label()


def read_project_name(project_id: str) -> str:
    """The name to SHOW for an id, falling back to the id — never a guessed name."""
    record = Project.load_or_none(project_id)
    return project_id if record is None else record.display_name()
