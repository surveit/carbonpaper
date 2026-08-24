"""The record base: what a stored row is, and the permission scope it declares."""
from __future__ import annotations

from enum import Enum
from uuid import uuid4
from typing import ClassVar, Iterator, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.ids import ID
from app.core.persistence import get_store
from app.core.json_types import JsonDict, JsonScalar
from app.core.timestamp_ids import now_iso


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

    id: ID = Field(default_factory=lambda: uuid4().hex, frozen=True)
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
    # Extra model_dump kwargs holding a subclass's on-disk shape. Never "mode".
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
    def load_raw(cls, id: ID) -> JsonDict:
        """The stored payload, unvalidated. Raises DocumentNotFound; a torn payload is not empty."""
        return get_store().read(cls.collection, id)

    @classmethod
    def load_raw_or_none(cls, id: ID) -> JsonDict | None:
        return get_store().read_tolerant(cls.collection, id)

    @classmethod
    def list_raw(cls, prefix: str = "") -> Iterator[tuple[str, JsonDict]]:
        """Payloads with their ids, unvalidated — for a reader that reports a bad record."""
        return get_store().read_all(cls.collection, prefix)

    @classmethod
    def list_ids(cls, prefix: str = "") -> list[ID]:
        """Ids alone, no document bodies — declared above list(), which shadows the builtin."""
        return get_store().list_ids(cls.collection, prefix)

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
