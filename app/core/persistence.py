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
from typing import Any, Callable, ClassVar, Iterator, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The one honest dynamic boundary: an arbitrary pydantic model_dump / JSON body.
JsonDict = dict[str, Any]
StoreUpdate = Callable[[JsonDict | None, int | None], tuple[JsonDict, int]]


def validate_id(id: str) -> str:
    """``:`` is tested directly, not via ``Path.is_absolute()``, which lets ``C:/x`` pass on Linux."""
    if not id or id != id.strip():
        raise ValueError(f"empty or untrimmed id: {id!r}")
    if id.startswith("/") or "\\" in id or "\x00" in id or ":" in id:
        raise ValueError(f"unsafe id: {id!r}")
    if any(part in ("", "..") for part in id.split("/")):
        raise ValueError(f"unsafe id (empty or '..' segment): {id!r}")
    return id


class DocumentStore(Protocol):
    def write(self, collection: str, id: str, data: JsonDict, schema_version: int = 1) -> None: ...
    def update(self, collection: str, id: str, mutate: StoreUpdate) -> None: ...
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

