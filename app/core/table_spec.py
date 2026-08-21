"""The column layout of one record type. See docs/models-and-storage.md."""
from __future__ import annotations

import enum
import inspect
import json
import typing
from typing import Any, Sequence

from pydantic import BaseModel


class Column(BaseModel):
    name: str
    sql_type: str
    nullable: bool
    # Held as a JSON string in a TEXT column: a list, a dict, or a nested model.
    is_json: bool


class TableSpec(BaseModel):
    table: str
    columns: list[Column]

    def create_statement(self) -> str:
        body = ",\n  ".join(
            f"{c.name} {c.sql_type}"
            + ("" if c.nullable or "PRIMARY KEY" in c.sql_type else " NOT NULL")
            for c in self.columns
        )
        return f"CREATE TABLE IF NOT EXISTS {self.table} (\n  {body}\n)"

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def build_row(self, id: str, data: dict[str, Any], schema_version: int) -> list[Any]:
        fixed = {"id": id, "schema_version": schema_version}
        return [fixed.get(c.name, _to_cell(c, data.get(c.name))) for c in self.columns]

    def read_row(self, row: Sequence[Any]) -> dict[str, Any]:
        return {c.name: json.loads(cell) if c.is_json and cell is not None else cell
                for c, cell in zip(self.columns, row) if c.name != "schema_version"}


def read_table_spec(table: str, fields: dict[str, Any]) -> TableSpec:
    """`fields` is a pydantic `model_fields`; `id` leads and carries the primary key."""
    columns = [
        Column(name="id", sql_type="TEXT PRIMARY KEY", nullable=False, is_json=False),
        # Store bookkeeping, not a model field: the blob row carried it, so the table does too.
        Column(name="schema_version", sql_type="INTEGER", nullable=False, is_json=False),
    ]
    for name, info in fields.items():
        if name == "id":
            continue
        sql_type, nullable, is_json = _read_column_shape(info.annotation)
        columns.append(Column(name=name, sql_type=sql_type, nullable=nullable, is_json=is_json))
    return TableSpec(table=table, columns=columns)


def _read_column_shape(annotation: Any) -> tuple[str, bool, bool]:
    args = typing.get_args(annotation)
    nullable = type(None) in args
    inner = [a for a in args if a is not type(None)]
    base = inner[0] if (nullable and len(inner) == 1) else annotation
    if typing.get_origin(base) in (list, dict, set, tuple):
        return "TEXT", nullable, True
    if inspect.isclass(base) and issubclass(base, enum.Enum):
        return "TEXT", nullable, False
    if base is bool or base is int:
        return "INTEGER", nullable, False
    if base is float:
        return "REAL", nullable, False
    if base is str:
        return "TEXT", nullable, False
    return "TEXT", nullable, True


def _to_cell(column: Column, value: Any) -> Any:
    return json.dumps(value) if column.is_json else value
