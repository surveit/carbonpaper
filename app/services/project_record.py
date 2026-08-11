"""The project identity record. `id` is a minted surrogate that never changes; `name`
is the label the directory carries and the reader types, and it may be changed.
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


def find_project_by_name(name: str) -> Project | None:
    """Reads every record — the store indexes by id prefix, which the name no longer is."""
    return next((record for record in Project.list() if record.name == name), None)


def resolve_project_id(name: str) -> str | None:
    """None means no record — which is a project the workspace holds only as a directory."""
    record = find_project_by_name(name)
    return None if record is None else record.id
