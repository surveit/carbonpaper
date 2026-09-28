"""What a pack registers. `register_pack` runs when the pack is imported and refuses a clash there."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.connectors import CONNECTORS, SOURCE_COLUMNS, ConnectorSpec
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stages.input_data import ConnectorKind
from app.models.stages.shared import INTERNAL_COLUMN_PREFIX


@dataclass(frozen=True)
class TourFixture:
    """A committed bundle, and what its input stages bind so a run reads the files committed with it."""

    bundle: Path
    bindings: Mapping[StageId, TypeUnsafeUserStageConfigOverride]


@dataclass(frozen=True)
class PackSpec:
    pack_id: str
    connectors: tuple[ConnectorSpec[Any], ...]
    tour: TourFixture | None = None


PACKS: dict[str, PackSpec] = {}


def register_pack(spec: PackSpec) -> None:
    _refuse_taken_names(spec)
    for connector in spec.connectors:
        _validate_metadata_columns(connector)
    PACKS[spec.pack_id] = spec
    CONNECTORS.update((connector.kind, connector) for connector in spec.connectors)


def _refuse_taken_names(spec: PackSpec) -> None:
    if spec.pack_id in PACKS:
        raise ValueError(f"pack {spec.pack_id!r} is already registered")
    kinds = [connector.kind for connector in spec.connectors]
    taken = {ConnectorKind.file.value, *CONNECTORS}
    clashing = sorted({kind for kind in kinds if kind in taken or kinds.count(kind) > 1})
    if clashing:
        raise ValueError(
            f"pack {spec.pack_id!r} registers connector kind(s) {clashing}, which another "
            "connector already holds")


def _validate_metadata_columns(connector: ConnectorSpec[Any]) -> None:
    taken = {column.name for column in SOURCE_COLUMNS}
    for column in connector.metadata_columns:
        if not column.nullable:
            raise ValueError(
                f"connector {connector.kind!r} declares metadata column {column.name!r} "
                "not nullable, but a file a run binds in its place has no metadata")
        if column.name.startswith(INTERNAL_COLUMN_PREFIX) or column.name in taken:
            raise ValueError(
                f"connector {connector.kind!r} declares metadata column {column.name!r}; a "
                f"name starting {INTERNAL_COLUMN_PREFIX!r}, a source column "
                f"({', '.join(c.name for c in SOURCE_COLUMNS)}) or a second use is refused")
        taken.add(column.name)
