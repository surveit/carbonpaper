"""A stored file and the project that holds it, as two records rather than one.
Splitting them is what lets the hold be refused, moved or dropped without the bytes
being touched, and what leaves the file itself with nothing project-shaped on it.
"""
from __future__ import annotations

from typing import ClassVar

from app.core.persistence import PersistedModel, PersistenceScope


class StoredFile(PersistedModel):
    """`sha256` addresses the BYTES, so two projects sending the same ones get a record each."""

    collection: ClassVar[str] = "uploaded_file"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    sha256: str
    filename: str
    byte_count: int


class ProjectFile(PersistedModel):
    """One file's one project. A file no edge names is held by nobody, which is a state."""

    collection: ClassVar[str] = "project_file"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project_id: str
    file_id: str
