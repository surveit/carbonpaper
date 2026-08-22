"""The project record, and the name to show for an id."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from app.core.persistence import PersistedModel, PersistenceScope
from app.services import workspace


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
    record = read_project_record(project_id)
    return project_id if record is None else record.display_name()


def read_project_record(project_id: str) -> Project | None:
    """Falls back to project.json: a project imported onto disk has no record to load."""
    record = Project.load_or_none(project_id)
    if record is not None:
        return record
    return read_project_json(project_id)


def resolve_project_json_path(project_id: str) -> Path:
    return workspace.resolve_project_dir(project_id) / "project.json"


def read_project_json(project_id: str) -> Project | None:
    pj = resolve_project_json_path(project_id)
    if not pj.is_file():
        return None
    try:
        stored = json.loads(pj.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(stored, dict):
        return None
    # project.json's `created_at` is the authoring date; the record's own stamps the write.
    return Project(
        id=project_id,
        name=stored.get("name"),
        title=stored.get("title"),
        model=stored.get("model"),
        source=stored.get("source"),
        authored_at=stored.get("created_at"),
    )
