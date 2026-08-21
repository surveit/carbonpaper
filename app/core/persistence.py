"""What a stored record is (``PersistedModel``) and the seam it reaches storage
through: the ``DocumentStore`` protocol and the process-wide handle. No engine and no
first-party import, which is what lets a contract in ``app.models`` import this module
while ``app.core.sqlite_store`` stays out of reach (import-linter, ``pyproject.toml``).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from threading import RLock
from uuid import uuid4
from typing import ClassVar, Iterator, Mapping, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.ids import ID
from app.core.json_types import JsonDict, JsonScalar
from app.core.run_lease import RunLeaseStore



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


class Store(DocumentStore, RunLeaseStore, Protocol):
    """One handle, because the fence reads a lease and writes a document in one transaction."""


_store: Store | None = None


def configure_store(store: Store) -> None:
    global _store
    _store = store


def get_store() -> Store:
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


class PersistenceScope(str, Enum):
    """Constrains only code running INSIDE a run; the authoring surface always has full access."""

    RUN = "run"
    PROJECT_READ = "project_read"
    PROJECT_READ_WRITE = "project_read_write"


class PersistedModel(BaseModel):
    """list() selects on an id PREFIX; find() selects on stored fields, so an id may stay opaque."""

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )

    id: ID = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

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
        self.updated_at = now_iso()
        get_store().write(
            self.collection,
            self.id,
            self.model_dump(mode="json", **self.DUMP_OPTS),
            schema_version=self.SCHEMA_VERSION,
        )

    @classmethod
    def load(cls, id: ID) -> Self:
        return cls.model_validate(get_store().read(cls.collection, id))

    @classmethod
    def load_or_none(cls, id: ID) -> Self | None:
        data = get_store().read_tolerant(cls.collection, id)
        return cls.model_validate(data) if data is not None else None

    @classmethod
    def find(cls, **fields: JsonScalar) -> list[Self]:
        """Matches on the STORED json, so an enum field takes its value and not the member."""
        return [cls.model_validate(data) for _, data
                in get_store().find(cls.collection, cls._resolve_stored_keys(fields))]

    @classmethod
    def list(cls, prefix: str = "") -> list[Self]:
        return [cls.model_validate(data)
                for _, data in get_store().read_all(cls.collection, prefix)]

    @classmethod
    def delete(cls, id: ID) -> None:
        get_store().delete(cls.collection, id)

    @classmethod
    def exists(cls, id: ID) -> bool:
        return get_store().exists(cls.collection, id)

    @classmethod
    def _resolve_stored_keys(cls, fields: Mapping[str, JsonScalar]) -> dict[str, JsonScalar]:
        # An unknown name would match nothing rather than fail, so it raises here.
        unknown = sorted(set(fields) - set(cls.model_fields))
        if unknown:
            raise ValueError(f"{cls.__name__} has no field(s) {unknown}")
        if not cls.DUMP_OPTS.get("by_alias"):
            return dict(fields)
        return {cls._read_dump_key(name): value for name, value in fields.items()}

    @classmethod
    def _read_dump_key(cls, name: str) -> str:
        field = cls.model_fields[name]
        return field.serialization_alias or field.alias or name

