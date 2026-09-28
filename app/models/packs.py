"""What a pack registers. `register_pack` runs when the pack is imported and refuses a clash there."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.connectors import CONNECTORS, SOURCE_COLUMNS, ConnectorSpec
from app.models.stages.input_data import ConnectorKind
from app.models.stages.shared import INTERNAL_COLUMN_PREFIX


@dataclass(frozen=True)
class PackSpec:
    pack_id: str
    connectors: tuple[ConnectorSpec[Any], ...]


PACKS: dict[str, PackSpec] = {}


def register_pack(spec: PackSpec) -> None:
    _refuse_taken_names(spec)
    for connector in spec.connectors:
        _refuse_metadata_the_kernel_owns(connector)
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


def _refuse_metadata_the_kernel_owns(connector: ConnectorSpec[Any]) -> None:
    taken = {column.name for column in SOURCE_COLUMNS}
    for column in connector.metadata_columns:
        if column.name.startswith(INTERNAL_COLUMN_PREFIX) or column.name in taken:
            raise ValueError(
                f"connector {connector.kind!r} declares metadata column {column.name!r}; a "
                f"name starting {INTERNAL_COLUMN_PREFIX!r}, a source column "
                f"({', '.join(c.name for c in SOURCE_COLUMNS)}) or a second use is refused")
        taken.add(column.name)
