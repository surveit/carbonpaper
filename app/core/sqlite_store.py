"""The SQLite ``DocumentStore``. See docs/models-and-storage.md."""
from __future__ import annotations

import json
from threading import RLock
from typing import Any, Iterator, Mapping

from sqlalchemy import create_engine, delete, insert, select
from sqlalchemy.engine import Row
from sqlalchemy.pool import StaticPool

from app.core.errors import DocumentNotFound
from app.core.persistence import JsonDict, JsonScalar
from app.core.table_spec import (
    DOCUMENTS, BlobRows, ColumnRows, StoredTable, find_table,
)


class SqliteKvStore:
    """ONE connection serves every caller across threads: any method touching it must hold `_lock`."""

    def __init__(self, db_path: str) -> None:
        self._lock = RLock()
        url = "sqlite://" if db_path == ":memory:" else f"sqlite:///{db_path}"
        # The only pool under which ":memory:" is ONE database, not one per connection.
        self._engine = create_engine(
            url, poolclass=StaticPool, connect_args={"check_same_thread": False})
        with self._engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        DOCUMENTS.create(self._engine, checkfirst=True)
        self._created: set[str] = {DOCUMENTS.name}

    def write(self, collection: str, id: str, data: JsonDict, schema_version: int = 1) -> None:
        stored = self._stored(collection)
        statement = insert(stored.table).prefix_with("OR REPLACE").values(
            **stored.build_row(id, data, schema_version))
        with self._lock, self._engine.begin() as connection:
            connection.execute(statement)

    def read(self, collection: str, id: str) -> JsonDict:
        stored = self._stored(collection)
        row = self._select_one(stored, id)
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        # Not the tolerant path: a corrupt blob raises here rather than reading as missing.
        return stored.read_body(row)

    def read_tolerant(self, collection: str, id: str) -> JsonDict | None:
        stored = self._stored(collection)
        row = self._select_one(stored, id)
        if row is None:
            return None
        try:
            return stored.read_body(row)
        except json.JSONDecodeError:
            return None

    def schema_version(self, collection: str, id: str) -> int:
        stored = self._stored(collection)
        statement = select(stored.table.c.schema_version).where(
            *stored.scope(), stored.table.c.id == id)
        row = self._fetch_one(statement)
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        return int(row.schema_version)

    def exists(self, collection: str, id: str) -> bool:
        stored = self._stored(collection)
        statement = select(stored.table.c.id).where(*stored.scope(), stored.table.c.id == id)
        return self._fetch_one(statement) is not None

    def delete(self, collection: str, id: str) -> None:
        stored = self._stored(collection)
        statement = delete(stored.table).where(*stored.scope(), stored.table.c.id == id)
        with self._lock, self._engine.begin() as connection:
            connection.execute(statement)

    def find(
        self, collection: str, fields: Mapping[str, JsonScalar]
    ) -> Iterator[tuple[str, JsonDict]]:
        stored = self._stored(collection)
        tests = [stored.match(name, value) for name, value in fields.items()]
        yield from self._read_rows(stored, self._rows_query(stored).where(*tests))

    def list_ids(self, collection: str, prefix: str = "") -> list[str]:
        stored = self._stored(collection)
        statement = select(stored.table.c.id).where(
            *stored.scope(), *_prefix_tests(stored, prefix)).order_by(stored.table.c.id)
        with self._lock, self._engine.connect() as connection:
            return [str(row.id) for row in connection.execute(statement)]

    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]:
        stored = self._stored(collection)
        query = self._rows_query(stored).where(*_prefix_tests(stored, prefix))
        yield from self._read_rows(stored, query)

    def _stored(self, collection: str) -> StoredTable:
        """Which table this collection lives in, and how a row of it maps to a document."""
        table = find_table(collection)
        if table is None:
            return BlobRows(collection)
        if table.name not in self._created:
            table.create(self._engine, checkfirst=True)
            self._created.add(table.name)
        return ColumnRows(table)

    def _rows_query(self, stored: StoredTable) -> Any:
        return select(stored.table).where(*stored.scope()).order_by(stored.table.c.id)

    def _select_one(self, stored: StoredTable, id: str) -> Row[Any] | None:
        return self._fetch_one(self._rows_query(stored).where(stored.table.c.id == id))

    def _fetch_one(self, statement: Any) -> Row[Any] | None:
        with self._lock, self._engine.connect() as connection:
            return connection.execute(statement).fetchone()

    def _read_rows(
        self, stored: StoredTable, statement: Any
    ) -> Iterator[tuple[str, JsonDict]]:
        with self._lock, self._engine.connect() as connection:
            rows = connection.execute(statement).fetchall()
        for row in rows:
            yield str(row.id), stored.read_body(row)


def _prefix_tests(stored: StoredTable, prefix: str) -> list[Any]:
    if not prefix:
        return []
    return [stored.table.c.id.startswith(prefix)]
