"""A project's methodology — the authored prose the workflow was compiled from.

Kept out of the Project identity record deliberately: the home page loads every
project's Project record to build its cards, and the prose is the largest thing
a project owns.
"""
from __future__ import annotations

from typing import ClassVar

from app.core.persistence import PersistedModel, PersistenceScope


class Methodology(PersistedModel):
    """One project's authored prose, `id`'d by project name."""

    collection: ClassVar[str] = "methodology"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    text: str


def read_methodology(project_id: str) -> str | None:
    """The project's prose, or None when it has none — never a stand-in."""
    record = Methodology.load_or_none(project_id)
    return record.text if record is not None else None


def write_methodology(project_id: str, text: str) -> None:
    """Raises ValueError on empty text — absence is stored as no record."""
    if not text.strip():
        raise ValueError("The methodology document is empty.")
    stored = Methodology.load_or_none(project_id)
    born = {"created_at": stored.created_at} if stored is not None else {}
    Methodology(id=project_id, text=text, **born).save()


def exists(project_id: str) -> bool:
    return Methodology.exists(project_id)
