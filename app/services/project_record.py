"""The name to show for a project id."""

from __future__ import annotations

from app.models.records.project import Project


def read_project_name(project_id: str) -> str:
    """The name to SHOW for an id, falling back to the id — never a guessed name."""
    record = Project.load_or_none(project_id)
    return project_id if record is None else record.display_name()
