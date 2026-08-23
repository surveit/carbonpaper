"""The seam a record reaches storage through: the protocol, the handle, the stamp."""
from __future__ import annotations

from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Iterator, Mapping, Protocol


from app.core.ids import ID

# The one honest dynamic boundary: an arbitrary pydantic model_dump / JSON body.
JsonDict = dict[str, Any]
# What a stored field can be compared against: a JSON scalar, never a list or object.
JsonScalar = str | int | float | bool | None


def validate_id(id: ID) -> ID:
    """``:`` is tested directly, not via ``Path.is_absolute()``, which lets ``C:/x`` pass on Linux."""
    if not id or id != id.strip():
        raise ValueError(f"empty or untrimmed id: {id!r}")
    if id.startswith("/") or "\\" in id or "\x00" in id or ":" in id:
        raise ValueError(f"unsafe id: {id!r}")
    if any(part in ("", "..") for part in id.split("/")):
        raise ValueError(f"unsafe id (empty or '..' segment): {id!r}")
    return id


class DocumentStore(Protocol):
    def write(self, collection: str, id: ID, data: JsonDict, schema_version: int = 1) -> None: ...
    def read(self, collection: str, id: ID) -> JsonDict: ...
    def read_tolerant(self, collection: str, id: ID) -> JsonDict | None: ...
    def exists(self, collection: str, id: ID) -> bool: ...
    def delete(self, collection: str, id: ID) -> None: ...
    def find(
        self, collection: str, fields: Mapping[str, JsonScalar]
    ) -> Iterator[tuple[str, JsonDict]]: ...
    def list_ids(self, collection: str, prefix: str = "") -> list[ID]: ...
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


def now_iso() -> str:
    # Strictly increasing WITHIN a process only — two processes can still tie in one OS tick.
    global _last_stamp
    with _stamp_lock:
        now = datetime.now()
        if _last_stamp is not None and now <= _last_stamp:
            now = _last_stamp + timedelta(microseconds=1)
        _last_stamp = now
    return now.isoformat(timespec="microseconds")
