"""The base of the document-storage seam: the DocumentStore protocol every
backend satisfies, and PersistedModel, the Active-Record base every stored
record subclasses. Backend-free by design — this module defines the storage
CONTRACT (write/read/exists/delete/list, keyed by collection + id) without
depending on any concrete implementation, so it sits at the bottom of the
import graph and imports nothing first-party — guarded by the import-linter
contract in ``pyproject.toml``.

``app.core.sqlite_store`` provides the concrete SQLite backend (the only
module that may ``import sqlite3`` — guarded by the executable seal in
``app/_arch_tests/test_storage_engine_sealed.py``); ``app.main`` wires one to
the other with a single ``configure_store()`` call at startup. Swapping the
backend (Postgres, or plain files for inspection) is a new ``DocumentStore``
implementation plus that one call; nothing above the seam changes.

Implementation status: ``validate_id``, ``DocumentStore``, and
``PersistedModel`` are implemented here; ``SqliteKvStore`` lives in
``app.core.sqlite_store``; ``FrameStore`` (app/core/frames.py) is the tabular
counterpart.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Iterator, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field

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


class PersistedModel(BaseModel):
    """Base for every stored record. A subclass sets `collection` (the table name)
    and carries an `id` (its primary key); save()/load()/list() go through the
    configured DocumentStore, so nothing above this class touches storage. The
    body is serialized as JSON.

    `created_at`/`updated_at` are stamped automatically, so a subclass never
    hand-rolls them: on a fresh construct (no stored value yet) both
    default_factory to now; on load from the store, the stored values are
    present in the input dict so the factory never fires, and the original
    values survive. `save()` re-stamps `updated_at` to now on every call, so it
    always reflects the last write while `created_at` stays at first-construct
    time.

    Its own strict config mirrors app.core.models._Base without importing it, so the
    storage layer stays free of an app.core.models dependency."""

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
