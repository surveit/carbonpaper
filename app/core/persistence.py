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
seam changes.

Implementation status: ``validate_id``, ``SqliteKvStore``, ``DocumentStore``, and
``PersistedModel`` are implemented; ``FrameStore`` lands next per the Phase-1 plan,
guarded by the arch checks above.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Iterator, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import DocumentNotFound

# The one honest dynamic boundary: an arbitrary pydantic model_dump / JSON body.
JsonDict = dict[str, Any]


def validate_id(id: str) -> str:
    """Return ``id`` if it is safe to use as a storage key and relative-path
    component, else raise ``ValueError``. A composite id (``<project>/<local>``)
    may contain ``/``, but never an empty or ``..`` segment, a leading ``/``, a
    backslash, a NUL, or a colon — so an id sourced from a model or an upload
    can't escape its collection when a backend turns it into a file path. This
    rejects an absolute path under any OS convention: POSIX-absolute (``/x``) is
    caught by the leading-``/`` check, and the colon ban catches every
    Windows-absolute form — drive-absolute (``C:/x``, ``C:\\x``) and
    drive-relative (``C:x``) alike, plus NTFS alternate-data-stream names
    (``name:stream``) — on every OS, including when validation runs on Linux.
    That last part matters because ``pathlib.Path(id).is_absolute()`` follows
    whatever platform it runs on and would let ``C:/x`` through unchanged there,
    so this check tests for ``:`` directly instead of deferring to pathlib."""
    if not id or id != id.strip():
        raise ValueError(f"empty or untrimmed id: {id!r}")
    if id.startswith("/") or "\\" in id or "\x00" in id or ":" in id:
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


def is_store_configured() -> bool:
    return _store is not None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PersistenceScope(str, Enum):
    """Permission profile for RUN ACTIVITY against project-scoped storage — not
    the authoring surface, which always has full project-scope read/write
    directly (a version, draft, or chat session is written by a human or an
    authoring agent, never by run code). This enum constrains only what code
    executing INSIDE a run may touch:

    - RUN: run-scope read/write only, no project-scope access at all — the
      record is produced by one run and is meaningless outside it.
    - AUTHORED: project-scope read at most, from a run — never write. A run
      may read a human-authored artifact (e.g. the version it executes) but
      never write one.
    - CROSS_RUN: project-scope read AND write — the only profile that grants
      run activity a write outliving the run. A model carrying this scope
      must define `for_mode`, the view that revokes that write for a
      non-production run (consumed by the eval/smoke run path *(planned)*).

    Design invariant: exactly one PersistedModel subclass may carry
    SCOPE = CROSS_RUN — the single deliberate cross-run channel; broadening it
    would blur the line this scope exists to hold. StageCacheEntry
    (app.services.stage_cache) is that one subclass; both the "every subclass
    declares SCOPE" rule and the "CROSS_RUN implies for_mode" rule are enforced
    by the arch tests in app/_arch_tests/test_persisted_models_declare_scope.py.
    """

    RUN = "run"
    CROSS_RUN = "cross_run"
    AUTHORED = "authored"


class PersistedModel(BaseModel):
    """Base for every stored record. A subclass sets `collection` (the table name)
    and carries an `id` (its primary key); save()/load()/list() go through the
    configured DocumentStore, so nothing above this class touches storage. The
    body is serialized as JSON. Every subclass also declares `SCOPE` — see
    `PersistenceScope` — with no base-class default, so an unannotated
    subclass fails at `save()` with a plain `AttributeError`.

    `created_at`/`updated_at` are stamped automatically, so a subclass never
    hand-rolls them: on a fresh construct (no stored value yet) both
    default_factory to now; on load from the store, the stored values are
    present in the input dict so the factory never fires, and the original
    values survive. `save()` re-stamps `updated_at` to now on every call, so it
    always reflects the last write while `created_at` stays at first-construct
    time.

    Its own strict config mirrors app.models._Base without importing it, so the
    storage layer stays free of an app.models dependency."""

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )

    id: str
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
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

