"""The single document-storage seam. Everything above speaks typed PersistedModel
objects; only this module (and app/core/frames.py, for tabular payloads) knows how
those objects reach storage — a SQLite key-value table.

Sealed on purpose, and the seal is executable:
  - no other module imports ``sqlite3`` — guarded by
    ``app/_arch_tests/test_storage_engine_sealed.py``;
  - the store sits at the bottom of the import graph: it imports ``app.core.errors``
    and nothing else first-party — guarded by the import-linter contract in
    ``pyproject.toml``.
Swapping the backend (Postgres, or plain files for inspection) is a new
DocumentStore implementation plus one ``configure_store`` call; nothing above the
seam changes. See ``docs/persistence-unification.md``.

Implementation status: this is the module shell. ``validate_id`` (Task 1) is here;
``SqliteKvStore`` / ``PersistedModel`` land next per the Phase-1 plan, guarded by
the arch checks above.
"""
from __future__ import annotations

from typing import Any

# The one honest dynamic boundary: an arbitrary pydantic model_dump / JSON body.
JsonDict = dict[str, Any]


def validate_id(id: str) -> str:
    """Return ``id`` if it is safe to use as a storage key and relative-path
    component, else raise ``ValueError``. A composite id (``<project>/<local>``)
    may contain ``/``, but never an empty or ``..`` segment, a leading ``/``, a
    backslash, or a NUL — so an id sourced from a model or an upload can't escape
    its collection when a backend turns it into a file path."""
    if not id or id != id.strip():
        raise ValueError(f"empty or untrimmed id: {id!r}")
    if id.startswith("/") or "\\" in id or "\x00" in id:
        raise ValueError(f"unsafe id: {id!r}")
    if any(part in ("", "..") for part in id.split("/")):
        raise ValueError(f"unsafe id (empty or '..' segment): {id!r}")
    return id

