"""SQLAlchemy tables built from the pydantic models. See docs/models-and-storage.md."""
from __future__ import annotations

import enum
import inspect
import json
import typing
from typing import Any, Protocol

from sqlalchemy import Column, Float, Integer, MetaData, Table, Text, func
from sqlalchemy.sql import ColumnElement

METADATA = MetaData()

# The blob table every collection used to live in, and most still do.
DOCUMENTS = Table(
    "documents", METADATA,
    Column("collection", Text, primary_key=True),
    Column("id", Text, primary_key=True),
    Column("data", Text, nullable=False),
    Column("schema_version", Integer, nullable=False, server_default="1"),
)


class StoredTable(Protocol):
    """How one collection maps onto a table. Two shapes: a blob row, or real columns."""

    table: Table

    def build_row(self, id: str, data: dict[str, Any], schema_version: int) -> dict[str, Any]: ...
    def read_body(self, row: Any) -> dict[str, Any]: ...
    def scope(self) -> list[ColumnElement[bool]]: ...
    def match(self, name: str, value: Any) -> ColumnElement[bool]: ...


class BlobRows:
    """One collection inside `documents`. Deleted when the last collection gets columns."""

    def __init__(self, collection: str) -> None:
        self.collection = collection
        self.table = DOCUMENTS

    def build_row(self, id: str, data: dict[str, Any], schema_version: int) -> dict[str, Any]:
        return {"collection": self.collection, "id": id,
                "data": json.dumps(data), "schema_version": schema_version}

    def read_body(self, row: Any) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(row.data)
        return body

    def scope(self) -> list[ColumnElement[bool]]:
        return [DOCUMENTS.c.collection == self.collection]

    def match(self, name: str, value: Any) -> ColumnElement[bool]:
        # json_extract reaches into the blob; a columnized table compares the column itself.
        reach = func.json_extract(DOCUMENTS.c.data, f"$.{name}")
        return reach.is_(None) if value is None else reach == value


class ColumnRows:
    def __init__(self, table: Table) -> None:
        self.table = table

    def build_row(self, id: str, data: dict[str, Any], schema_version: int) -> dict[str, Any]:
        fixed = {"id": id, "schema_version": schema_version}
        return {c.name: fixed.get(c.name, _to_cell(c, data.get(c.name))) for c in self.table.c}

    def read_body(self, row: Any) -> dict[str, Any]:
        mapped = row._mapping
        return {c.name: json.loads(mapped[c.name]) if _is_json(c) and mapped[c.name] is not None
                else mapped[c.name]
                for c in self.table.c if c.name != "schema_version"}

    def scope(self) -> list[ColumnElement[bool]]:
        return []

    def match(self, name: str, value: Any) -> ColumnElement[bool]:
        if name not in self.table.c:
            raise KeyError(f"{self.table.name} has no column {name!r}")
        column = self.table.c[name]
        return column.is_(None) if value is None else column == value


def find_table(collection: str) -> Table | None:
    """METADATA is the registry: `build_table` puts a table in it by constructing one."""
    table = METADATA.tables.get(collection)
    return None if table is DOCUMENTS else table


def build_table(name: str, fields: dict[str, Any]) -> Table:
    columns: list[Column[Any]] = [
        Column("id", Text, primary_key=True),
        # Store bookkeeping, not a model field: the blob row carried it, so the table does too.
        Column("schema_version", Integer, nullable=False),
    ]
    for field, info in fields.items():
        if field == "id":
            continue
        columns.append(_build_column(field, info.annotation))
    return Table(name, METADATA, *columns)


def _build_column(name: str, annotation: Any) -> Column[Any]:
    args = typing.get_args(annotation)
    nullable = type(None) in args
    inner = [a for a in args if a is not type(None)]
    base = inner[0] if (nullable and len(inner) == 1) else annotation
    if typing.get_origin(base) in (list, dict, set, tuple):
        return Column(name, Text, nullable=nullable, info={"json": True})
    if inspect.isclass(base) and issubclass(base, enum.Enum):
        return Column(name, Text, nullable=nullable)
    if base is bool or base is int:
        return Column(name, Integer, nullable=nullable)
    if base is float:
        return Column(name, Float, nullable=nullable)
    if base is str:
        return Column(name, Text, nullable=nullable)
    return Column(name, Text, nullable=nullable, info={"json": True})


def _is_json(column: Column[Any]) -> bool:
    return bool(column.info.get("json"))


def _to_cell(column: Column[Any], value: Any) -> Any:
    return json.dumps(value) if _is_json(column) else value
