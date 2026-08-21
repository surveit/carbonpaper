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
from app.core.persistence import JsonDict, JsonScalar, RunLease
from app.core.ids import ID


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
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS run_lease ("
            "  run_id TEXT PRIMARY KEY,"
            "  executor_id TEXT NOT NULL,"
            "  fence INTEGER NOT NULL,"
            "  expires_at INTEGER NOT NULL)"
        )
        self._conn.commit()

    def write(self, collection: str, id: ID, data: JsonDict, schema_version: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
                "VALUES (?, ?, ?, ?)",
                (collection, id, json.dumps(data), schema_version),
            )
            self._conn.commit()

    def read(self, collection: str, id: ID) -> JsonDict:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM documents WHERE collection=? AND id=?", (collection, id)
            ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        parsed: JsonDict = json.loads(row[0])
        return parsed

    def schema_version(self, collection: str, id: ID) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT schema_version FROM documents WHERE collection=? AND id=?",
                (collection, id),
            ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        return int(row[0])

    def exists(self, collection: str, id: ID) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM documents WHERE collection=? AND id=?", (collection, id)
            ).fetchone()
        return row is not None

    def delete(self, collection: str, id: ID) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM documents WHERE collection=? AND id=?", (collection, id)
            )
            self._conn.commit()

    def read_tolerant(self, collection: str, id: ID) -> JsonDict | None:
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

    def list_ids(self, collection: str, prefix: str = "") -> list[ID]:
        return [str(row[0]) for row in self._scan("id", collection, prefix)]

    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]:
        for row_id, data in self._scan("id, data", collection, prefix):
            body: JsonDict = json.loads(data)
            yield str(row_id), body

    # --- execution leases: docs/run-leases.md --------------------------------

    def claim_lease(self, run_id: ID, executor_id: str, ttl_seconds: int) -> RunLease | None:
        """None when a live lease is held by someone else. One statement, so two racers cannot tie."""
        with self._lock:
            row = self._conn.execute(
                "INSERT INTO run_lease (run_id, executor_id, fence, expires_at) "
                "VALUES (?, ?, 1, unixepoch() + ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "  executor_id = excluded.executor_id,"
                "  fence = run_lease.fence + 1,"
                "  expires_at = excluded.expires_at "
                "WHERE run_lease.expires_at <= unixepoch() "
                "RETURNING run_id, executor_id, fence, expires_at",
                (run_id, executor_id, ttl_seconds),
            ).fetchone()
            self._conn.commit()
        return _read_lease_row(row)

    def renew_lease(self, lease: RunLease, ttl_seconds: int) -> RunLease | None:
        """None once taken over — the holder's signal to stop. Renewing never moves the fence."""
        with self._lock:
            row = self._conn.execute(
                "UPDATE run_lease SET expires_at = unixepoch() + ? "
                "WHERE run_id=? AND executor_id=? AND fence=? "
                "RETURNING run_id, executor_id, fence, expires_at",
                (ttl_seconds, lease.run_id, lease.executor_id, lease.fence),
            ).fetchone()
            self._conn.commit()
        return _read_lease_row(row)

    def release_lease(self, lease: RunLease) -> None:
        """Only our own tenure: a lease already taken over belongs to someone still using it."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM run_lease WHERE run_id=? AND fence=?", (lease.run_id, lease.fence)
            )
            self._conn.commit()

    def read_lease(self, run_id: ID) -> RunLease | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, executor_id, fence, expires_at FROM run_lease WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return _read_lease_row(row)

    def store_now(self) -> int:
        """The clock every lease deadline is set by, so a reader never compares its own."""
        with self._lock:
            return int(self._conn.execute("SELECT unixepoch()").fetchone()[0])

    def write_if_held(
        self, collection: str, id: ID, data: JsonDict, lease: RunLease, schema_version: int = 1
    ) -> bool:
        """The fence, atomic under `_lock`: False means the lease moved on, so this write is refused."""
        with self._lock:
            held = self._conn.execute(
                "SELECT 1 FROM run_lease WHERE run_id=? AND fence=? AND expires_at > unixepoch()",
                (lease.run_id, lease.fence),
            ).fetchone()
            if held is None:
                return False
            self._conn.execute(
                "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
                "VALUES (?, ?, ?, ?)",
                (collection, id, json.dumps(data), schema_version),
            )
            self._conn.commit()
        return True


def _read_lease_row(row: tuple[Any, ...] | None) -> RunLease | None:
    if row is None:
        return None
    return RunLease(run_id=str(row[0]), executor_id=str(row[1]),
                    fence=int(row[2]), expires_at=int(row[3]))
