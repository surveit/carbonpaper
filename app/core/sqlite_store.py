"""The concrete SQLite backend for the document store: one table, opaque JSON
bodies keyed by (collection, id). No other module may import ``sqlite3`` —
guarded by the executable seal in
``app/_arch_tests/test_storage_engine_sealed.py`` — so this is the one place
the app talks to a database.

``app.core.persistence`` defines the storage contract this class satisfies
(the ``DocumentStore`` protocol) and ``PersistedModel``, the base every stored
record subclasses, without depending on this backend. ``app.main`` wires the
two together with one ``configure_store()`` call at startup; swapping backends
(Postgres, or plain files for inspection) is a new ``DocumentStore``
implementation plus that one call, and nothing above the seam changes.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterator

from app.core.errors import DocumentNotFound
from app.core.persistence import JsonDict


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
