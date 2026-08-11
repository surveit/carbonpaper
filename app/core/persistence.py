"""The single document-storage seam (a SQLite key-value table), sealed by two
executable checks: no other module may import ``sqlite3``
(``app/_arch_tests/test_storage_engine_sealed.py``), and this module must import
``app.core.errors`` and nothing else first-party (import-linter contract in
``pyproject.toml``).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from enum import Enum
from threading import RLock
from uuid import uuid4
from typing import Any, ClassVar, Iterator, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.errors import DocumentNotFound

# The one honest dynamic boundary: an arbitrary pydantic model_dump / JSON body.
JsonDict = dict[str, Any]


def validate_id(id: str) -> str:
    """``:`` is tested directly, not via ``Path.is_absolute()``, which lets ``C:/x`` pass on Linux."""
    if not id or id != id.strip():
        raise ValueError(f"empty or untrimmed id: {id!r}")
    if id.startswith("/") or "\\" in id or "\x00" in id or ":" in id:
        raise ValueError(f"unsafe id: {id!r}")
    if any(part in ("", "..") for part in id.split("/")):
        raise ValueError(f"unsafe id (empty or '..' segment): {id!r}")
    return id


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

    def list_ids(self, collection: str, prefix: str = "") -> list[str]:
        return [str(row[0]) for row in self._scan("id", collection, prefix)]

    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]:
        for row_id, data in self._scan("id, data", collection, prefix):
            body: JsonDict = json.loads(data)
            yield str(row_id), body


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
    global _store
    _store = store


def get_store() -> DocumentStore:
    if _store is None:
        raise RuntimeError("document store not configured; call configure_store() first")
    return _store


def is_store_configured() -> bool:
    return _store is not None


_stamp_lock = RLock()
_last_stamp: datetime | None = None


def _now_iso() -> str:
    # Strictly increasing WITHIN a process only — two processes can still tie in one OS tick.
    global _last_stamp
    with _stamp_lock:
        now = datetime.now()
        if _last_stamp is not None and now <= _last_stamp:
            now = _last_stamp + timedelta(microseconds=1)
        _last_stamp = now
    return now.isoformat(timespec="microseconds")


class PersistenceScope(str, Enum):
    """Constrains only code running INSIDE a run; the authoring surface always has full access."""

    RUN = "run"
    PROJECT_READ = "project_read"
    PROJECT_READ_WRITE = "project_read_write"


class PersistedModel(BaseModel):
    """list() selects by id PREFIX only, so a per-project record must compose id as project/local."""

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )

    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    @model_validator(mode="after")
    def _stamp_one_creation_instant(self) -> Self:
        if not {"created_at", "updated_at"} & self.model_fields_set:
            object.__setattr__(self, "updated_at", self.created_at)
        return self
    collection: ClassVar[str]
    SCOPE: ClassVar[PersistenceScope]
    SCHEMA_VERSION: ClassVar[int] = 1
    # Extra model_dump kwargs a subclass needs to preserve exact on-disk shape
    # (e.g. {"by_alias": True, "exclude_none": True} for a stage-bearing record).
    # Must not include "mode" — that is fixed to "json".
    DUMP_OPTS: ClassVar[JsonDict] = {}

    def save(self) -> None:
        self.updated_at = _now_iso()
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

