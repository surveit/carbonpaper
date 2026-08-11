"""The project identity record. `id` is the project — it is also the name of its
directory under the projects root — and it never changes. `name` is a display label
that may change and is NOT unique: two projects may carry the same one.
Lookup by name reads the whole collection, so nothing may call it in a loop.
"""

from __future__ import annotations

from typing import ClassVar

from app.core.persistence import PersistedModel, PersistenceScope
from app.core.timestamp_ids import mint_timestamp_id


class Project(PersistedModel):
    """`authored_at` is the project's own date; `created_at` stamps when this RECORD was written."""

    collection: ClassVar[str] = "project"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    name: str
    title: str | None = None
    model: str | None = None
    source: str | None = None
    authored_at: str | None = None


def mint_project_id() -> str:
    return mint_timestamp_id()


def find_projects_by_name(name: str) -> list[Project]:
    """Plural because `name` is not unique — reads every record, so never call it in a loop."""
    return [record for record in Project.list() if record.name == name]


def describe_project(project_id: str) -> str:
    """The label to SHOW for an id, falling back to the id — never a guessed name."""
    record = Project.load_or_none(project_id)
    return project_id if record is None else record.name
