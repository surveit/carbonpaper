"""Create a project's working copy and its record together, as create_project does."""
from __future__ import annotations

from pathlib import Path

from app.models.records.project import Project
from app.services.workspace import resolve_project_dir


def seed_project(project_id: str, *, name: str | None = None) -> Path:
    directory = resolve_project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    Project(id=project_id, name=name or project_id).save()
    return directory
