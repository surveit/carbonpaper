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

Implementation status: ``validate_id`` and ``SqliteKvStore`` are implemented;
``PersistedModel`` and the ``DocumentStore`` protocol land next per the Phase-1
plan, guarded by the arch checks above.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import DocumentNotFound

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


class SqliteKvStore:
    """DocumentStore backed by one SQLite table: opaque JSON bodies keyed by
    (collection, id). Writes are atomic; WAL mode lets readers run concurrently
    with a writer. `db_path` is a file path or ":memory:" (tests)."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            "  collection TEXT NOT NULL,"
            "  id TEXT NOT NULL,"
            "  data TEXT NOT NULL,"
            "  schema_version INTEGER NOT NULL DEFAULT 1,"
            "  PRIMARY KEY (collection, id))"
        )
        self._conn.commit()

    def write(self, collection: str, id: str, data: JsonDict, schema_version: int = 1) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
            "VALUES (?, ?, ?, ?)",
            (collection, id, json.dumps(data), schema_version),
        )
        self._conn.commit()

    def read(self, collection: str, id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT data FROM documents WHERE collection=? AND id=?", (collection, id)
        ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        parsed: JsonDict = json.loads(row[0])
        return parsed

    def schema_version(self, collection: str, id: str) -> int:
        row = self._conn.execute(
            "SELECT schema_version FROM documents WHERE collection=? AND id=?",
            (collection, id),
        ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        return int(row[0])

