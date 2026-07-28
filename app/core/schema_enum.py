"""The string-enum base for values that travel on a JSON-schema wire."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema


class SchemaEnum(str, Enum):
    """A `str` enum whose JSON schema carries no Pydantic-generated `title`. The
    title only ever repeats the class name, which is already the `$defs` key the
    schema is filed under — pure duplication on every wire copy. Subclass it
    wherever a plain `str, Enum` would do; members and comparisons are
    unchanged."""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        schema: dict[str, Any] = dict(handler(core_schema))
        schema.pop("title", None)
        return schema
