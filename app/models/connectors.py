"""A connector kind a pack registers, and the Source columns the kernel writes for each file it reads."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Generic, TypeVar

from pydantic import AfterValidator, Field

from app.models.base import _Base
from app.models.schema import Column


def _refuse_a_relative_path(path: str) -> str:
    if not path.strip():
        raise ValueError("a connector path must be a non-empty string")
    if not Path(path).is_absolute():
        raise ValueError(f"a connector path must be ABSOLUTE, got {path!r}")
    return path


AbsolutePath = Annotated[str, AfterValidator(_refuse_a_relative_path)]


# Every kind's params extend this, so a run binds files the same way whatever the kind.
class ConnectorParams(_Base):
    paths: list[AbsolutePath] = Field(default_factory=list)


SOURCE_ID_COLUMN = "source_id"
SOURCE_SHA256_COLUMN = "source_sha256"

MetadataValue = str | int | float | bool | date | None


@dataclass(frozen=True)
class AcquiredBytes:
    """One file a connector reached. The kernel opens it, stores it and stamps the time."""

    filename: str
    origin_url: str
    open_bytes: Callable[[], BinaryIO]
    metadata: Mapping[str, MetadataValue]


@dataclass(frozen=True)
class MirroredBytes(AcquiredBytes):
    """Bytes a mirror kept, with the time and sha256 it recorded when it fetched them."""

    fetched_at: str
    sha256: str


KindParams = TypeVar("KindParams", bound=ConnectorParams)


@dataclass(frozen=True)
class ConnectorSpec(Generic[KindParams]):
    """A kind whose rows are its files: the SOURCE_COLUMNS, then `metadata_columns`."""

    kind: str
    params_model: type[KindParams]
    metadata_columns: tuple[Column, ...]
    acquire: Callable[[KindParams], Iterator[AcquiredBytes]]


SOURCE_COLUMNS: tuple[Column, ...] = (
    Column(name=SOURCE_ID_COLUMN, type="str", nullable=False),
    Column(name=SOURCE_SHA256_COLUMN, type="str", nullable=False),
    Column(name="filename", type="str", nullable=False),
    # None for an uploaded file a run binds in place of acquiring.
    Column(name="origin_url", type="str", nullable=True),
    Column(name="fetched_at", type="str", nullable=True),
)

CONNECTORS: dict[str, ConnectorSpec[Any]] = {}


def find_connector(kind: str) -> ConnectorSpec[Any]:
    if kind not in CONNECTORS:
        raise ValueError(
            f"connector kind {kind!r} is not 'file' and no pack registers it; "
            f"pack kinds: {sorted(CONNECTORS)}")
    return CONNECTORS[kind]
