"""The SQLite ``DocumentStore``, sealed as the only module that imports ``sqlite3``
(``app/_arch_tests/test_storage_engine_sealed.py``). Nothing but a composition root
names it: everything else speaks the protocol in ``app.core.persistence``.
"""
from __future__ import annotations

import json
import sqlite3
from threading import RLock
from typing import Any, Iterator, Mapping

from app.core.errors import DocumentNotFound, TableSchemaMismatch
from app.core.persistence import JsonDict, JsonScalar, find_table_spec
from app.core.table_spec import TableSpec


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
        self._checked_tables: set[str] = set()

    def write(self, collection: str, id: str, data: JsonDict, schema_version: int = 1) -> None:
        spec = self._table_for(collection)
        if spec is not None:
            self._write_row(spec, id, data, schema_version)
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
                "VALUES (?, ?, ?, ?)",
                (collection, id, json.dumps(data), schema_version),
            )
            self._conn.commit()

    def read(self, collection: str, id: str) -> JsonDict:
        spec = self._table_for(collection)
        if spec is not None:
            row = self._select_row(spec, id)
            if row is None:
                raise DocumentNotFound(f"{collection}/{id}")
            return _read_row(spec, row)
        with self._lock:
            found = self._conn.execute(
                "SELECT data FROM documents WHERE collection=? AND id=?", (collection, id)
            ).fetchone()
        if found is None:
            raise DocumentNotFound(f"{collection}/{id}")
        # Not read_tolerant's path: a corrupt blob raises here rather than reading as missing.
        parsed: JsonDict = json.loads(found[0])
        return parsed

    def schema_version(self, collection: str, id: str) -> int:
        spec = self._table_for(collection)
        source = ("documents", "collection=? AND id=?", (collection, id)) if spec is None \
            else (spec.table, "id=?", (id,))
        with self._lock:
            row = self._conn.execute(
                f"SELECT schema_version FROM {source[0]} WHERE {source[1]}", source[2],
            ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        return int(row[0])

    def exists(self, collection: str, id: str) -> bool:
        spec = self._table_for(collection)
        with self._lock:
            if spec is not None:
                row = self._conn.execute(
                    f"SELECT 1 FROM {spec.table} WHERE id=?", (id,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM documents WHERE collection=? AND id=?", (collection, id)
                ).fetchone()
        return row is not None

    def delete(self, collection: str, id: str) -> None:
        spec = self._table_for(collection)
        with self._lock:
            if spec is not None:
                self._conn.execute(f"DELETE FROM {spec.table} WHERE id=?", (id,))
            else:
                self._conn.execute(
                    "DELETE FROM documents WHERE collection=? AND id=?", (collection, id)
                )
            self._conn.commit()

    def read_tolerant(self, collection: str, id: str) -> JsonDict | None:
        spec = self._table_for(collection)
        if spec is not None:
            row = self._select_row(spec, id)
            return None if row is None else _read_row(spec, row)
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
        spec = self._table_for(collection)
        if spec is not None:
            yield from self._find_rows(spec, fields)
            return
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
        spec = self._table_for(collection)
        if spec is not None:
            return [str(row[0]) for row in self._scan_table(spec, "id", prefix)]
        return [str(row[0]) for row in self._scan("id", collection, prefix)]

    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]:
        spec = self._table_for(collection)
        if spec is not None:
            for row in self._scan_table(spec, ", ".join(spec.column_names()), prefix):
                yield str(row[0]), _read_row(spec, row)
            return
        for row_id, data in self._scan("id, data", collection, prefix):
            body: JsonDict = json.loads(data)
            yield str(row_id), body

    def _table_for(self, collection: str) -> TableSpec | None:
        """None means the collection still lives as a blob row in `documents`."""
        spec = find_table_spec(collection)
        if spec is not None and spec.table not in self._checked_tables:
            self._ensure_table(spec)
        return spec

    def _ensure_table(self, spec: TableSpec) -> None:
        with self._lock:
            self._conn.execute(spec.create_statement())
            self._conn.commit()
            found = [str(row[1]) for row in
                     self._conn.execute(f"PRAGMA table_info({spec.table})").fetchall()]
        if found != spec.column_names():
            raise TableSchemaMismatch(
                f"table {spec.table} holds columns {found} but the record declares "
                f"{spec.column_names()} — the record changed shape and no migration moved "
                f"the table with it")
        self._checked_tables.add(spec.table)

    def _select_row(self, spec: TableSpec, id: str) -> tuple[Any, ...] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(spec.column_names())} FROM {spec.table} WHERE id=?", (id,)
            ).fetchone()
        return None if row is None else tuple(row)

    def _write_row(self, spec: TableSpec, id: str, data: JsonDict, schema_version: int) -> None:
        values = spec.build_row(id, data, schema_version)
        names = ", ".join(spec.column_names())
        marks = ", ".join("?" * len(spec.columns))
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {spec.table} ({names}) VALUES ({marks})", values)
            self._conn.commit()

    def _find_rows(
        self, spec: TableSpec, fields: Mapping[str, JsonScalar]
    ) -> Iterator[tuple[str, JsonDict]]:
        known = set(spec.column_names())
        unknown = sorted(set(fields) - known)
        if unknown:
            raise TableSchemaMismatch(f"{spec.table} has no column(s) {unknown}")
        tests, params = [], []
        for name, value in fields.items():
            # A column name cannot be bound, so it is interpolated — checked above.
            tests.append(f"{name} IS NULL" if value is None else f"{name} = ?")
            if value is not None:
                params.append(value)
        where = f" WHERE {' AND '.join(tests)}" if tests else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(spec.column_names())} FROM {spec.table}{where} ORDER BY id",
                params,
            ).fetchall()
        for row in rows:
            yield str(row[0]), _read_row(spec, row)

    def _scan_table(self, spec: TableSpec, columns: str, prefix: str) -> list[tuple[Any, ...]]:
        with self._lock:
            if prefix:
                hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
                return self._conn.execute(
                    f"SELECT {columns} FROM {spec.table} WHERE id>=? AND id<? ORDER BY id",
                    (prefix, hi),
                ).fetchall()
            return self._conn.execute(
                f"SELECT {columns} FROM {spec.table} ORDER BY id").fetchall()


def _read_row(spec: TableSpec, row: tuple[Any, ...]) -> JsonDict:
    body: JsonDict = spec.read_row(row)
    return body
