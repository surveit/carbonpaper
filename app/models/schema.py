"""Schema primitives — the model base and the anonymous Column / TableSchema.

The pieces both the workflow (`stage.py`) and the named data model (`named_schemas.py`)
build on: the model base, the column-type vocabulary, `Column`, `TableSchema` (an
anonymous schema that can be declared inline), the `SourceRef` provenance handle,
and the error formatter. They live *below* both modules — `stage.py` and
`named_schemas.py` import from here, never the other way around — so `NamedColumn`
and `NamedSchema` can extend `Column`/`TableSchema` without `named_schemas.py`
depending on `stage.py`.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)


# ── Base ─────────────────────────────────────────────────────────────────────
class _Base(BaseModel):
    """Contract base. Unknown keys are rejected — a typo'd field is an invalid
    stage, not silently-ignored data. Enum-typed fields hold their plain string
    value after validation (compare with `==`, never `is`; never call `.value`).
    Defaults are validated like any other value, and fields can be populated by
    python name or alias."""
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )


# ── Identifiers ──────────────────────────────────────────────────────────────
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# ── Column-type vocabulary ───────────────────────────────────────────────────
SCALAR_COLUMN_TYPES: set[str] = {
    "str", "int", "float", "bool", "datetime", "date", "dict", "json",
}
_LIST_RE = re.compile(r"^list\[(.+)\]$")


def is_valid_column_type(t: str) -> bool:
    """Scalar, or list[<scalar>] / nested list[list[...]]."""
    if not isinstance(t, str):
        return False
    if t in SCALAR_COLUMN_TYPES:
        return True
    m = _LIST_RE.match(t)
    if m:
        inner = m.group(1).strip()
        return inner in SCALAR_COLUMN_TYPES or bool(_LIST_RE.match(inner))
    return False


# ── Provenance ───────────────────────────────────────────────────────────────
class SourceRef(_Base):
    """Where a stage's or schema's prose justification lives."""
    doc: Optional[str] = None
    section: Optional[str] = None
    lines: Optional[list[int]] = None


# ── Typed columns / schemas ──────────────────────────────────────────────────
class Column(_Base):
    name: str
    type: str = "str"
    nullable: bool = True
    description: Optional[str] = None
    range: Optional[list[Any]] = None
    source: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if not is_valid_column_type(v):
            raise ValueError(f"unknown column type {v!r}")
        return v


class TableSchema(_Base):
    """An anonymous schema — columns plus an optional primary key — that can be
    declared inline (e.g. a stage's `output_schema`). `NamedSchema` promotes it to
    a first-class, named artifact."""
    # Sequence (covariant), not list: subclasses narrow the element type
    # (NamedSchema.columns is list[NamedColumn]), which an invariant list
    # would forbid. Pydantic still validates/stores a list at runtime.
    columns: Sequence[Column]
    estimated_rows: Optional[int] = None
    primary_key: Optional[list[str]] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "TableSchema":
        seen: set[str] = set()
        for c in self.columns:
            if c.name in seen:
                raise ValueError(f"duplicate column {c.name!r}")
            seen.add(c.name)
        for k in self.primary_key or []:
            if k not in seen:
                raise ValueError(f"primary_key {k!r} is not a declared column")
        return self


# ── Error formatting ─────────────────────────────────────────────────────────
def format_errors(err: ValidationError) -> list[str]:
    """Pydantic errors → human-readable issue strings."""
    out: list[str] = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "stages")
        msg = e.get("msg", "invalid")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out
