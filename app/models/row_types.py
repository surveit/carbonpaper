"""A RowType is the word for what one row IS; a NamedSchema is a table shape that HAS one."""
from __future__ import annotations

from pydantic import ConfigDict, Field, field_validator

from app.core.ids import ID
from app.models.schema import _Base, _SNAKE_RE
from app.models.tool_schema_prompts import (
    ROW_TYPE_ALSO_WRITTEN_DESCRIPTION,
    ROW_TYPE_DESCRIPTION,
)


class RowType(_Base):
    model_config = ConfigDict(json_schema_extra={"description": ROW_TYPE_DESCRIPTION})

    id: ID
    title: str
    definition: str
    also_written: list[str] = Field(
        default_factory=list, description=ROW_TYPE_ALSO_WRITTEN_DESCRIPTION
    )

    @field_validator("id")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"id {v!r} should be snake_case")
        return v
