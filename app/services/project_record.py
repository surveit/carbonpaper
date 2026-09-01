"""Reads off a project's own record: the name to show it by, and when it changed."""

from __future__ import annotations

from datetime import datetime

from app.core.timestamp_ids import read_orderable_stamp
from app.models.records.project import Project


def read_project_name(project_id: str) -> str:
    """The name to SHOW for an id, falling back to the id — never a guessed name."""
    record = Project.load_or_none(project_id)
    return project_id if record is None else record.display_name()


def read_project_edited_at(project_id: str) -> datetime | None:
    """Renames and creation only — the stage specs are the working copy's to date."""
    raw = Project.load_raw_or_none(project_id)
    return None if raw is None else read_orderable_stamp(raw.get("updated_at"))
