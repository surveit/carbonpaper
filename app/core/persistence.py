"""The seam a record reaches storage through: the protocol and the process handle."""
from __future__ import annotations

from typing import Iterator, Mapping, Protocol

from app.core.ids import ID
from app.core.json_types import JsonDict, JsonScalar



class StoreProtocol(Protocol):
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


_store: StoreProtocol | None = None


def configure_store(store: StoreProtocol) -> None:
    global _store
    _store = store


def get_store() -> StoreProtocol:
    if _store is None:
        raise RuntimeError("store not configured; call configure_store() first")
    return _store


def is_store_configured() -> bool:
    return _store is not None
