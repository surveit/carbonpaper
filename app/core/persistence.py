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

Implementation status: ``validate_id``, ``SqliteKvStore``, ``DocumentStore``, and
``PersistedModel`` are implemented; ``FrameStore`` lands next per the Phase-1 plan,
guarded by the arch checks above.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, ClassVar, Iterator, Protocol, Self

from pydantic import BaseModel, ConfigDict

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

    def exists(self, collection: str, id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM documents WHERE collection=? AND id=?", (collection, id)
        ).fetchone()
        return row is not None

    def delete(self, collection: str, id: str) -> None:
        self._conn.execute(
            "DELETE FROM documents WHERE collection=? AND id=?", (collection, id)
        )
        self._conn.commit()

    def read_tolerant(self, collection: str, id: str) -> JsonDict | None:
        row = self._conn.execute(
            "SELECT data FROM documents WHERE collection=? AND id=?", (collection, id)
        ).fetchone()
        if row is None:
            return None
        try:
            parsed: JsonDict = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return parsed

    def _scan(self, columns: str, collection: str, prefix: str) -> sqlite3.Cursor:
        # `columns` is an internal literal, never user input. Prefix match is an
        # index-friendly range on the (collection, id) primary key.
        if prefix:
            hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            return self._conn.execute(
                f"SELECT {columns} FROM documents "
                "WHERE collection=? AND id>=? AND id<? ORDER BY id",
                (collection, prefix, hi),
            )
        return self._conn.execute(
            f"SELECT {columns} FROM documents WHERE collection=? ORDER BY id",
            (collection,),
        )

    def list_ids(self, collection: str, prefix: str = "") -> list[str]:
        return [row[0] for row in self._scan("id", collection, prefix)]

    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]:
        for row_id, data in self._scan("id, data", collection, prefix):
            body: JsonDict = json.loads(data)
            yield row_id, body


class DocumentStore(Protocol):
    def write(self, collection: str, id: str, data: JsonDict, schema_version: int = 1) -> None: ...
    def read(self, collection: str, id: str) -> JsonDict: ...
    def read_tolerant(self, collection: str, id: str) -> JsonDict | None: ...
    def exists(self, collection: str, id: str) -> bool: ...
    def delete(self, collection: str, id: str) -> None: ...
    def list_ids(self, collection: str, prefix: str = "") -> list[str]: ...
    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]: ...


_store: DocumentStore | None = None


def configure_store(store: DocumentStore) -> None:
    """Install the process-wide document store. App startup calls this once with a
    SqliteKvStore('data/app.db'); each test installs a fresh SqliteKvStore(':memory:')."""
    global _store
    _store = store


def get_store() -> DocumentStore:
    if _store is None:
        raise RuntimeError("document store not configured; call configure_store() first")
    return _store


class PersistedModel(BaseModel):
    """Base for every stored record. A subclass sets `collection` (the table name)
    and carries an `id` (its primary key); save()/load()/list() go through the
    configured DocumentStore, so nothing above this class touches storage. The
    body is serialized as JSON (see docs/persistence-unification.md).

    Its own strict config mirrors app.core.models._Base without importing it, so the
    storage layer stays free of an app.core.models dependency."""

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )

    id: str
    collection: ClassVar[str]
    SCHEMA_VERSION: ClassVar[int] = 1
    # Extra model_dump kwargs a subclass needs to preserve exact on-disk shape
    # (e.g. {"by_alias": True, "exclude_none": True} for a stage-bearing record).
    # Must not include "mode" — that is fixed to "json".
    DUMP_OPTS: ClassVar[JsonDict] = {}

    def save(self) -> None:
        get_store().write(
            self.collection,
            self.id,
            self.model_dump(mode="json", **self.DUMP_OPTS),
            schema_version=self.SCHEMA_VERSION,
        )

    @classmethod
    def load(cls, id: str) -> Self:
        return cls.model_validate(get_store().read(cls.collection, id))

    @classmethod
    def load_or_none(cls, id: str) -> Self | None:
        data = get_store().read_tolerant(cls.collection, id)
        return cls.model_validate(data) if data is not None else None

    @classmethod
    def list(cls, prefix: str = "") -> list[Self]:
        return [cls.model_validate(data)
                for _, data in get_store().read_all(cls.collection, prefix)]

    @classmethod
    def delete(cls, id: str) -> None:
        get_store().delete(cls.collection, id)

    @classmethod
    def exists(cls, id: str) -> bool:
        return get_store().exists(cls.collection, id)

