"""The SQLite ``DocumentStore``, sealed as the only module that imports ``sqlite3``
(``app/_arch_tests/test_storage_engine_sealed.py``). Nothing but a composition root
names it: everything else speaks the protocol in ``app.core.persistence``.
"""
from __future__ import annotations

import json
import sqlite3
from threading import RLock
from typing import Any, Iterator, Mapping

from app.core.errors import DocumentNotFound
from app.core.persistence import JsonDict, JsonScalar


class SqliteKvStore:
    """ONE connection serves every caller across threads: any method touching it must hold `_lock`."""

    def __init__(self, db_path: str) -> None:
        self._lock = RLock()
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
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
                "VALUES (?, ?, ?, ?)",
                (collection, id, json.dumps(data), schema_version),
            )
            self._conn.commit()

    def read(self, collection: str, id: str) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM documents WHERE collection=? AND id=?", (collection, id)
            ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        parsed: JsonDict = json.loads(row[0])
        return parsed

    def schema_version(self, collection: str, id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT schema_version FROM documents WHERE collection=? AND id=?",
                (collection, id),
            ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        return int(row[0])

    def exists(self, collection: str, id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM documents WHERE collection=? AND id=?", (collection, id)
            ).fetchone()
        return row is not None

    def delete(self, collection: str, id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM documents WHERE collection=? AND id=?", (collection, id)
            )
            self._conn.commit()

    def read_tolerant(self, collection: str, id: str) -> JsonDict | None:
        with self._lock:
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

    def _scan(self, columns: str, collection: str, prefix: str) -> list[tuple[Any, ...]]:
        """`columns` is interpolated into the SQL: an internal literal only, never user input."""
        with self._lock:
            if prefix:
                hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
                return self._conn.execute(
                    f"SELECT {columns} FROM documents "
                    "WHERE collection=? AND id>=? AND id<? ORDER BY id",
                    (collection, prefix, hi),
                ).fetchall()
            return self._conn.execute(
                f"SELECT {columns} FROM documents WHERE collection=? ORDER BY id",
                (collection,),
            ).fetchall()

    def find(
        self, collection: str, fields: Mapping[str, JsonScalar]
    ) -> Iterator[tuple[str, JsonDict]]:
        # None matches a stored null and an absent key alike: json_extract cannot tell them apart.
        tests: list[str] = ["collection=?"]
        params: list[JsonScalar] = [collection]
        for name, value in fields.items():
            path = f"$.{name}"
            if value is None:
                tests.append("json_extract(data, ?) IS NULL")
                params.append(path)
            else:
                tests.append("json_extract(data, ?) = ?")
                params.extend((path, value))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, data FROM documents WHERE {' AND '.join(tests)} ORDER BY id",
                params,
            ).fetchall()
        for row_id, data in rows:
            body: JsonDict = json.loads(data)
            yield str(row_id), body

    def list_ids(self, collection: str, prefix: str = "") -> list[str]:
        return [str(row[0]) for row in self._scan("id", collection, prefix)]

    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]:
        for row_id, data in self._scan("id, data", collection, prefix):
            body: JsonDict = json.loads(data)
            yield str(row_id), body
