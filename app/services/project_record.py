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

    # Optional, and it must stay so: a project created before labels existed carries no
    # `name` key, and PersistedModel.load is a strict extra="forbid" validate, so a
    # required field would orphan every one of them. None is not a missing label — it
    # means the id is still the only name the project has, which `label` reports.
    name: str | None = None
    title: str | None = None
    model: str | None = None
    source: str | None = None
    authored_at: str | None = None

    def label(self) -> str:
        return self.name or self.id


def mint_project_id() -> str:
    return mint_timestamp_id()


def find_projects_by_name(name: str) -> list[Project]:
    """Plural because a label is not unique — reads every record, so never call it in a loop."""
    return [record for record in Project.list() if record.label() == name]


def describe_project(project_id: str) -> str:
    """The label to SHOW for an id, falling back to the id — never a guessed name."""
    record = Project.load_or_none(project_id)
    return project_id if record is None else record.label()
