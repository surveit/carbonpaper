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
SCALAR_COLUMN_TYPES: set[str] = {"str", "int", "float", "bool", "datetime", "date"}
STRUCTURED_COLUMN_TYPES: set[str] = {"json"}
_LIST_RE = re.compile(r"^list\[(.+)\]$")


def is_valid_column_type(t: str) -> bool:
    """Scalar, `json`, or `list[X]` where X is scalar / `json` / a nested
    `list[...]`."""
    if not isinstance(t, str):
        return False
    if t in SCALAR_COLUMN_TYPES or t in STRUCTURED_COLUMN_TYPES:
        return True
    m = _LIST_RE.match(t)
    if m:
        inner = m.group(1).strip()
        return (
            inner in SCALAR_COLUMN_TYPES
            or inner in STRUCTURED_COLUMN_TYPES
            or bool(_LIST_RE.match(inner))
        )
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
    enum: Optional[list[str]] = None
    fields: Optional[list["Column"]] = None
    value_type: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if not is_valid_column_type(v):
            raise ValueError(f"unknown column type {v!r}")
        return v

    @model_validator(mode="after")
    def _enum_only_on_str(self) -> "Column":
        if self.enum is not None:
            if self.type != "str":
                raise ValueError(
                    f"column {self.name!r}: enum is only valid on type 'str' "
                    f"(got {self.type!r})"
                )
            if len(self.enum) == 0:
                raise ValueError(f"column {self.name!r}: enum must be non-empty")
        return self

    @model_validator(mode="after")
    def _json_shape(self) -> "Column":
        is_json = self.type == "json" or self.type == "list[json]"
        if is_json:
            if self.fields is None and self.value_type is None:
                raise ValueError(
                    f"column {self.name!r}: a 'json'/'list[json]' column must "
                    "declare exactly one of 'fields' or 'value_type'"
                )
            if self.fields is not None and self.value_type is not None:
                raise ValueError(
                    f"column {self.name!r}: a 'json'/'list[json]' column must "
                    "declare exactly one of 'fields' or 'value_type', not both"
                )
            if self.value_type is not None and self.value_type not in SCALAR_COLUMN_TYPES:
                raise ValueError(
                    f"column {self.name!r}: value_type {self.value_type!r} must "
                    f"be one of {sorted(SCALAR_COLUMN_TYPES)}"
                )
        else:
            if self.fields is not None:
                raise ValueError(
                    f"column {self.name!r}: 'fields' is only valid on type "
                    f"'json' or 'list[json]' (got {self.type!r})"
                )
            if self.value_type is not None:
                raise ValueError(
                    f"column {self.name!r}: 'value_type' is only valid on type "
                    f"'json' or 'list[json]' (got {self.type!r})"
                )
        return self

    @model_validator(mode="after")
    def _range_is_numeric_bounds(self) -> "Column":
        """`range` is a numeric [low, high] bounds pair, valid only on int/float
        columns. A categorical string vocabulary is declared with `enum`, not
        `range`."""
        if self.range is None:
            return self
        if self.type not in ("int", "float"):
            raise ValueError(
                f"column {self.name!r}: range is only valid on numeric "
                f"(int/float) columns (got {self.type!r}) — use enum for a "
                "categorical string vocabulary"
            )
        if len(self.range) != 2 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in self.range
        ):
            raise ValueError(
                f"column {self.name!r}: range must be exactly two numbers "
                f"[low, high], got {self.range!r}"
            )
        return self


Column.model_rebuild()


# ── Column spec-equality ─────────────────────────────────────────────────────
# A producer's output column and a consumer's declared copy of it must match on
# SPEC, but may legitimately differ in prose (description/source). The spec
# fields are everything the Column model declares except its identity (`name`)
# and prose (`description`, `source`) — derived from the model, so a newly added
# capability is compared automatically instead of being silently ignored.
_PROSE_COLUMN_FIELDS = frozenset({"name", "description", "source"})
_SPEC_COLUMN_FIELDS: tuple[str, ...] = tuple(
    f for f in Column.model_fields if f not in _PROSE_COLUMN_FIELDS
)


def _column_spec_differences(a: Column, b: Column) -> list[str]:
    """Spec fields on which `a` and `b` differ (empty ⇒ same spec). Prose never
    counts, at any nesting level; `fields` recurses, so a nested prose-only
    difference is likewise ignored."""
    diffs: list[str] = []
    for field_name in _SPEC_COLUMN_FIELDS:
        if field_name == "fields":
            if not _fields_spec_equal(a.fields, b.fields):
                diffs.append("fields")
        elif getattr(a, field_name) != getattr(b, field_name):
            diffs.append(field_name)
    return diffs


def _fields_spec_equal(a: Optional[list[Column]], b: Optional[list[Column]]) -> bool:
    """Whether two nested-object `fields` lists describe the same shape by spec,
    matching sub-columns by name and comparing each with `_column_spec_differences`."""
    if a is None or b is None:
        return a is None and b is None
    a_by_name = {c.name: c for c in a}
    b_by_name = {c.name: c for c in b}
    if a_by_name.keys() != b_by_name.keys():
        return False
    return all(
        not _column_spec_differences(a_by_name[name], b_by_name[name])
        for name in a_by_name
    )


# ── Column type wording (shared by to_prompt) ───────────────────────────────
_SCALAR_TYPE_WORDING: dict[str, str] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "date": "ISO date string",
    "datetime": "ISO datetime string",
}


def _type_wording(t: str) -> str:
    """English wording for a scalar or `list[<scalar>]` column type, for use
    in prompts and messages. A `json`/`list[json]` column is rendered
    recursively from its `fields`/`value_type` instead (see `_render_column`),
    so this function is never called with those types."""
    if t in _SCALAR_TYPE_WORDING:
        return _SCALAR_TYPE_WORDING[t]
    m = _LIST_RE.match(t)
    if m:
        return f"array of {_type_wording(m.group(1).strip())} values"
    raise ValueError(f"unknown column type {t!r}")


def _numeric_range(col: Column) -> Optional[tuple[float, float]]:
    """`col.range` as a (low, high) bounds pair, or None when the column
    declares no range. `Column._range_is_numeric_bounds` guarantees a declared
    range is exactly two numbers on an int/float column."""
    if col.range is None:
        return None
    lo, hi = col.range
    return (lo, hi)


def _render_column(col: Column, indent: str) -> list[str]:
    """One `"name": <shape> (required...)[ — description]` line for `col`,
    followed by indented sub-lines when the shape is a nested object (a
    `json`/`list[json]` column with `fields`)."""
    requiredness = "required, never null" if not col.nullable else "or null"
    header = f'{indent}"{col.name}":'
    sub_lines: list[str] = []

    if col.type == "json" or col.type == "list[json]":
        is_array = col.type == "list[json]"
        if col.fields is not None:
            if is_array:
                shape = "an array of objects, each with keys:"
            else:
                shape = "an object with keys:"
            line = f"{header} {shape} ({requiredness})"
            if col.description:
                line += f" — {col.description}"
            sub_lines.append(line)
            child_indent = indent + "  "
            for field in col.fields:
                sub_lines.extend(_render_column(field, child_indent))
            return sub_lines
        assert col.value_type is not None  # enforced by Column._json_shape
        value_word = _SCALAR_TYPE_WORDING.get(col.value_type, col.value_type)
        if is_array:
            shape = f"an array of objects, each mapping string keys to {value_word} values"
        else:
            shape = f"an object mapping string keys to {value_word} values"
        line = f"{header} {shape} ({requiredness})"
        if col.description:
            line += f" — {col.description}"
        return [line]

    line = f"{header} {_type_wording(col.type)} ({requiredness})"
    if col.enum is not None:
        line += f" — one of: {' | '.join(col.enum)}"
    bounds = _numeric_range(col)
    if bounds is not None:
        line += f", between {bounds[0]} and {bounds[1]} inclusive"
    if col.description:
        line += f" — {col.description}"
    return [line]


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

    def subtract(self, other: "TableSchema") -> "TableSchema":
        """The columns of `self` whose names are not in `other`, as a schema
        describing a reply object (no primary key or table-level metadata).

        A column present in both must be spec-identical — every Column spec
        field (`type`, `nullable`, `range`, `enum`, `fields`, `value_type`, and
        any future capability), compared recursively via
        `_column_spec_differences`; only prose (`description`/`source`) may
        differ, since it legitimately differs between a producer's declaration
        and a consumer's copy — otherwise this raises ValueError naming the
        column and the differing fields."""
        other_by_name = {c.name: c for c in other.columns}
        remaining: list[Column] = []
        for c in self.columns:
            shared = other_by_name.get(c.name)
            if shared is None:
                remaining.append(c)
                continue
            differences = _column_spec_differences(c, shared)
            if differences:
                raise ValueError(
                    f"column {c.name!r} differs between schemas on "
                    f"{', '.join(differences)}: {shared!r} vs {c!r}"
                )
        return TableSchema(columns=remaining, primary_key=None)

    def to_prompt(self) -> str:
        """Render this schema as instructions for an LLM reply: one line per
        column stating its type, required/nullable wording, enum, range, and
        description — recursing into a `json`/`list[json]` column's `fields`
        as indented sub-lines, or naming its `value_type` for an open map —
        plus header/footer lines naming the expected JSON shape."""
        lines = [
            "Return ONE JSON object only — no prose, no code fences — "
            "with exactly these keys:"
        ]
        for c in self.columns:
            lines.extend(_render_column(c, ""))
        lines.append("Any other key is invalid.")
        return "\n".join(lines)


# ── Error formatting ─────────────────────────────────────────────────────────
def format_errors(err: ValidationError) -> list[str]:
    """Pydantic errors → human-readable issue strings."""
    out: list[str] = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "stages")
        msg = e.get("msg", "invalid")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out
