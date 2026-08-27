# Sits BELOW stage.py and named_schemas.py: they import from here, never the other
# way round, so NamedColumn/NamedSchema can extend Column/TableSchema.
from __future__ import annotations

import datetime
import re
from enum import Enum
from typing import Any, ClassVar, Literal, Optional, Sequence, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)
from app.core.ids import ID
from app.models.tool_schema_prompts import (
    SOURCE_REF_DESCRIPTION,
    TABLE_SCHEMA_DESCRIPTION,
)


# ── Base ─────────────────────────────────────────────────────────────────────
class _Base(BaseModel):
    """An enum-typed field holds a plain string after validation: compare with `==`, not `is`."""
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )


class StageConfig(_Base):
    """Every field of a subclass must appear in exactly one of these two sets; a test enforces it."""
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]]
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]]


# ── Identifiers ──────────────────────────────────────────────────────────────
_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# A stage's id, as other stages reference it (an entry in `inputs`, a stage-test's
# per-input rows, a signature's reads). An alias rather than a NewType so stage
# dicts parse unchanged.
StageId: TypeAlias = ID

# One stage's connector params for one run, as the caller supplied them. Nothing
# type-checks the keys or the values against the stage's config: the caller is
# responsible for knowing what that config makes available and overriding it correctly,
# and a wrong key surfaces only when the stage validates its connector.
TypeUnsafeUserStageConfigOverride: TypeAlias = dict[str, Any]


# Below `stage.py`, so a config class in its own module can use it without a cycle.
class FunctionKind(str, Enum):
    inline = "inline"


# ── Column-type vocabulary ───────────────────────────────────────────────────
SCALAR_COLUMN_TYPES: set[str] = {"str", "int", "float", "bool", "datetime", "date"}
STRUCTURED_COLUMN_TYPES: set[str] = {"json"}
_LIST_RE = re.compile(r"^list\[(.+)\]$")

# Named constants for the column-type values compared individually below (by
# _annotation_for/_render_column in this module, and by app.runtime.validation)
# — as opposed to the scalar/structured *sets* above, which are membership-tested
# as a whole.
STR_COLUMN_TYPE = "str"
JSON_COLUMN_TYPE = "json"
LIST_JSON_COLUMN_TYPE = "list[json]"

# The column types holding a python date/datetime, never the ISO string that spells one.
DATE_COLUMN_TYPES: frozenset[str] = frozenset({"date", "datetime"})


def find_list_element_type(t: str) -> str | None:
    """None when `t` is not a list type — the caller decides whether that is an error."""
    m = _LIST_RE.match(t)
    return m.group(1).strip() if m else None


def is_valid_column_type(t: str) -> bool:
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
    model_config = ConfigDict(json_schema_extra={"description": SOURCE_REF_DESCRIPTION})

    doc: Optional[str] = None
    section: Optional[str] = None
    lines: Optional[list[int]] = None


# The substring a `range` bound string carries to mean "unbounded on this
# side" (e.g. "+inf", "-inf") — recognized here and by the matching check in
# app/runtime/validation.py when it validates row data against a declared
# range.
RANGE_UNBOUNDED_MARKER = "inf"


def _is_range_bound(v: Any) -> bool:
    if isinstance(v, str):
        return RANGE_UNBOUNDED_MARKER in v
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ── Typed columns / schemas ──────────────────────────────────────────────────
class Column(_Base):
    name: str
    type: str = Field(
        description=(
            "Column type: a scalar (str, int, float, bool, date, datetime); `json` (a nested "
            "object — give its shape with `fields`, or an open string->scalar map with "
            "`value_type`); or `list[X]` of any of these (e.g. list[str], list[json])."
        ),
    )
    nullable: bool
    description: Optional[str] = None
    range: Optional[list[Any]] = None
    source: Optional[str] = None
    enum: Optional[list[str]] = Field(
        default=None,
        description=(
            "The closed set of values a `str` column may hold — declare it whenever the "
            "vocabulary is fixed and known at authoring time. A stage whose output holds "
            "a value outside the set FAILS, so declare it only where the set really is "
            "closed."
        ),
    )
    fields: Optional[list["Column"]] = Field(
        default=None,
        description=(
            "Sub-columns of a `json`/`list[json]` column. Declare exactly ONE of "
            "`fields` or `value_type`. Both are CHECKED against the data: a wrong "
            "declaration fails the stage."
        ),
    )
    value_type: Optional[str] = Field(
        default=None,
        description=(
            "Scalar value type for an open `json` map (string keys -> scalars). "
            "Alternative to `fields`."
        ),
    )

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if not is_valid_column_type(v):
            raise ValueError(f"unknown column type {v!r}")
        return v

    @model_validator(mode="after")
    def _enum_only_on_str(self) -> "Column":
        if self.enum is not None:
            if self.type != STR_COLUMN_TYPE:
                raise ValueError(
                    f"column {self.name!r}: enum is only valid on type 'str' "
                    f"(got {self.type!r})"
                )
            if len(self.enum) == 0:
                raise ValueError(f"column {self.name!r}: enum must be non-empty")
        return self

    @model_validator(mode="after")
    def _json_shape(self) -> "Column":
        is_json = self.type == JSON_COLUMN_TYPE or self.type == LIST_JSON_COLUMN_TYPE
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
        if self.range is None:
            return self
        if self.type not in ("int", "float"):
            raise ValueError(
                f"column {self.name!r}: range is only valid on numeric "
                f"(int/float) columns (got {self.type!r}) — use enum for a "
                "categorical string vocabulary"
            )
        if len(self.range) != 2 or not all(_is_range_bound(v) for v in self.range):
            raise ValueError(
                f"column {self.name!r}: range must be exactly two numbers "
                f"[low, high] (a bound may be a string containing \"inf\" for "
                f"unbounded), got {self.range!r}"
            )
        return self

    def resolve_numeric_bounds(self) -> tuple[float | None, float | None]:
        if self.range is None or self.type not in ("int", "float"):
            return (None, None)
        low, high = self.range
        return (None if isinstance(low, str) else low,
                None if isinstance(high, str) else high)


Column.model_rebuild()


# ── Column spec-equality ─────────────────────────────────────────────────────
# A producer's output column and a consumer's declared copy of it must match on
# SPEC, but may legitimately differ in prose (description/source). The spec
# fields are everything the Column model declares except its identity (`name`)
# and prose (`description`, `source`) — read off the model, so a newly added
# capability is compared automatically instead of being silently ignored.
_PROSE_COLUMN_FIELDS = frozenset({"name", "description", "source"})
_SPEC_COLUMN_FIELDS: tuple[str, ...] = tuple(
    f for f in Column.model_fields if f not in _PROSE_COLUMN_FIELDS
)


def _column_spec_differences(a: Column, b: Column) -> list[str]:
    diffs: list[str] = []
    for field_name in _SPEC_COLUMN_FIELDS:
        if field_name == "fields":
            if not _fields_spec_equal(a.fields, b.fields):
                diffs.append("fields")
        elif getattr(a, field_name) != getattr(b, field_name):
            diffs.append(field_name)
    return diffs


def _fields_spec_equal(a: Optional[list[Column]], b: Optional[list[Column]]) -> bool:
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
    if t in _SCALAR_TYPE_WORDING:
        return _SCALAR_TYPE_WORDING[t]
    m = _LIST_RE.match(t)
    if m:
        return f"array of {_type_wording(m.group(1).strip())} values"
    raise ValueError(f"unknown column type {t!r}")


def _numeric_range(col: Column) -> Optional[tuple[float, float]]:
    if col.range is None:
        return None
    lo, hi = col.range
    return (lo, hi)


def _render_column(col: Column, indent: str) -> list[str]:
    requiredness = "required, never null" if not col.nullable else "or null"
    header = f'{indent}"{col.name}":'
    sub_lines: list[str] = []

    if col.type == JSON_COLUMN_TYPE or col.type == LIST_JSON_COLUMN_TYPE:
        is_array = col.type == LIST_JSON_COLUMN_TYPE
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
    model_config = ConfigDict(json_schema_extra={"description": TABLE_SCHEMA_DESCRIPTION})

    # Sequence, not list: covariant, so NamedSchema can narrow columns to list[NamedColumn].
    columns: Sequence[Column]
    estimated_rows: Optional[int] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "TableSchema":
        seen: set[str] = set()
        for c in self.columns:
            if c.name in seen:
                raise ValueError(f"duplicate column {c.name!r}")
            seen.add(c.name)
        return self

    def column_for_name(self, name: str) -> Optional[Column]:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def extend(self, rewrites: Sequence[Column], adds: Sequence[Column]) -> "TableSchema":
        rewrites_by_name = {column.name: column for column in rewrites}
        return TableSchema(columns=[
            *(rewrites_by_name.get(column.name, column) for column in self.columns),
            *adds,
        ])

    def subtract(self, other: "TableSchema", strict: bool = True) -> "TableSchema":
        self_by_name = {c.name: c for c in self.columns}
        if strict and not other.is_subset_of(self):
            problems = [
                f"{c.name!r} differs on {', '.join(_column_spec_differences(c, self_by_name[c.name]))}"
                if c.name in self_by_name
                else f"{c.name!r} is absent from the minuend"
                for c in other.columns
                if c.name not in self_by_name or _column_spec_differences(c, self_by_name[c.name])
            ]
            raise ValueError(f"cannot subtract: {'; '.join(problems)}")
        other_by_name = {c.name: c for c in other.columns}
        return TableSchema(
            columns=[
                c for c in self.columns
                if (match := other_by_name.get(c.name)) is None
                or _column_spec_differences(c, match)
            ],
        )

    def differing_column_names(self, other: "TableSchema") -> set[str]:
        self_by_name = {c.name: c for c in self.columns}
        other_by_name = {c.name: c for c in other.columns}
        names = set(self_by_name) | set(other_by_name)
        return {
            name for name in names
            if name not in self_by_name
            or name not in other_by_name
            or _column_spec_differences(self_by_name[name], other_by_name[name])
        }

    def is_subset_of(self, other: "TableSchema") -> bool:
        return not self.find_unsatisfied_columns(other, nullability="exact")

    def find_unsatisfied_columns(
        self, producer: "TableSchema", nullability: Literal["compatible", "exact"] = "compatible"
    ) -> list[str]:
        producer_by_name = {c.name: c for c in producer.columns}
        reasons: list[str] = []
        for required in self.columns:
            supplied = producer_by_name.get(required.name)
            if supplied is None:
                reasons.append(f"column {required.name!r} absent from producer")
                continue
            diffs = _column_spec_differences(required, supplied)
            if nullability == "compatible" and "nullable" in diffs:
                diffs = [d for d in diffs if d != "nullable"]
                if required.nullable is False and supplied.nullable is True:
                    diffs.append("nullable (producer may emit null; column is required non-null)")
            if diffs:
                reasons.append(f"column {required.name!r} differs on {', '.join(diffs)}")
        return reasons

    def to_prompt(self) -> str:
        lines = [
            "Return ONE JSON object only — no prose, no code fences — "
            "with exactly these keys:"
        ]
        for c in self.columns:
            lines.extend(_render_column(c, ""))
        lines.append("Any other key is invalid.")
        return "\n".join(lines)

    def to_pydantic_model(self, name: str) -> type[BaseModel]:
        """Every column is a REQUIRED field: `nullable` permits a None value, not an absent key."""
        return _build_row_model(name, self.columns)


# ── Compiling to a Pydantic model (TableSchema.to_pydantic_model) ───────────
# Named consumer: app.runtime.stages.llm_transform compiles a stage's reply
# spec and hands the model to app.core.agent.agent.Agent as `target_schema`,
# so the reply spec is enforced (the agent must submit a validating
# instance) rather than merely described in prompt prose.
_SCALAR_PY_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "date": datetime.date,
    "datetime": datetime.datetime,
}


def _build_row_model(name: str, columns: Sequence[Column]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for column in columns:
        annotation = _annotation_for(column, parent_name=name)
        if column.nullable:
            annotation = Optional[annotation]
        field_definitions[column.name] = (annotation, _field_for(column))
    return create_model(
        name, __config__=ConfigDict(extra="forbid"), **field_definitions
    )


def _annotation_for(column: Column, parent_name: str) -> Any:
    if column.type in (JSON_COLUMN_TYPE, LIST_JSON_COLUMN_TYPE):
        inner: Any
        if column.fields is not None:
            inner = _build_row_model(f"{parent_name}__{column.name}", column.fields)
        else:
            assert column.value_type is not None  # Column._json_shape enforces
            scalar_py_type: Any = _SCALAR_PY_TYPES[column.value_type]
            inner = dict[str, scalar_py_type]
        return list[inner] if column.type == LIST_JSON_COLUMN_TYPE else inner
    if column.enum is not None:
        return Literal.__getitem__(tuple(column.enum))
    return _scalar_or_list_annotation(column.type)


def _scalar_or_list_annotation(type_name: str) -> Any:
    if type_name in _SCALAR_PY_TYPES:
        return _SCALAR_PY_TYPES[type_name]
    match = _LIST_RE.match(type_name)
    if match:
        element_type: Any = _scalar_or_list_annotation(match.group(1).strip())
        return list[element_type]
    raise ValueError(f"unknown column type {type_name!r}")


def _field_for(column: Column) -> Any:
    kwargs: dict[str, Any] = {}
    if column.description:
        kwargs["description"] = column.description
    low, high = column.resolve_numeric_bounds()
    if low is not None:
        kwargs["ge"] = low
    if high is not None:
        kwargs["le"] = high
    return Field(**kwargs)
