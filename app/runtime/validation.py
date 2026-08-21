"""Schema validation for stage I/O: column presence, type coercion, enum
vocabularies, range constraints, nullability, and primary-key uniqueness against
the output schema a stage's signature resolves to. Results are returned as
structured records, not raised.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
from pydantic import BaseModel, ValidationError

from app.core.utils import format_errors
from app.core.frames import (
    collapse_null_forms,
    CELL_TYPE_PREDICATES,
    is_schema_type_satisfied_by_arrow_type,
    find_arrow_list_value_type,
    is_null_form,
    is_sequence_cell,
)
from app.models import (
    Column,
    JSON_COLUMN_TYPE,
    LIST_JSON_COLUMN_TYPE,
    RANGE_UNBOUNDED_MARKER,
    SCALAR_COLUMN_TYPES,
    STR_COLUMN_TYPE,
    TableSchema,
)
from app.models.severity import UserFacingErrorSeverity


# ── Type checking ────────────────────────────────────────────────────────────
# The value-level "may this cell sit in a column declared as T?" predicates
# live in app.core.frames, which owns the pandas knowledge they are made of
# (numpy scalars, extension dtypes, null forms). What lives here is the part
# that needs the DECLARED-TYPE VOCABULARY — `json`, `list[X]`, the scalar set —
# which is app.models domain knowledge that app.core is forbidden to import
# (pyproject.toml: "app.core does not import the domain models"). So frames
# keys its predicates on plain strings and this module, the one place that can
# see both sides, pins the two vocabularies together.
_LIST_TYPE_RE = re.compile(r"^list\[(.+)\]$")

# Keep the vocabulary honest: every scalar the models accept must have a check.
assert set(CELL_TYPE_PREDICATES) == SCALAR_COLUMN_TYPES, (
    "app.core.frames.CELL_TYPE_PREDICATES drifted from "
    f"SCALAR_COLUMN_TYPES: {SCALAR_COLUMN_TYPES ^ set(CELL_TYPE_PREDICATES)}"
)

# How many offending values to name in an Issue message.
_OFFENDER_SAMPLE_N = 10


@dataclass
class Issue:
    severity: str    # UserFacingErrorSeverity.error | UserFacingErrorSeverity.warning
    column: str | None
    message: str


@dataclass
class ValidationReport:
    stage_id: str
    phase: str       # "input" | "output" | "claim"
    rows: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == UserFacingErrorSeverity.error for i in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "phase": self.phase,
            "rows": self.rows,
            "ok": self.ok,
            "issues": [
                {"severity": i.severity, "column": i.column, "message": i.message}
                for i in self.issues
            ],
        }


def validate_table(
    table: pa.Table,
    schema: TableSchema | None,
    *,
    stage_id: str,
    phase: str,
) -> ValidationReport:
    report = ValidationReport(stage_id=stage_id, phase=phase, rows=table.num_rows)
    if schema is None:
        # A stage declaring no schema emits files rather than a table.
        return report

    columns: list[Column] = list(schema.columns)
    present = set(table.column_names)
    report.issues.extend(_find_missing_declared_columns(present, columns))

    for col in columns:
        if col.name not in present:
            continue
        # One argument, not a values/type pair: a ChunkedArray carries both, so
        # nothing here can be handed a type belonging to some other column.
        values = table.column(col.name)
        report.issues.extend(_find_nullability_issues(values, col))
        report.issues.extend(_find_type_issues(values, col))
        report.issues.extend(_find_numeric_range_issues(values, col))
        report.issues.extend(_find_enum_issues(values, col))
        report.issues.extend(_find_json_shape_issues(values, col))

    report.issues.extend(
        _find_undeclared_columns(table.column_names, [c.name for c in columns])
    )
    return report


def build_row_model(schema: TableSchema, name: str) -> type[BaseModel]:
    """Range is warning-severity on a frame, so it must not fail a row."""
    return TableSchema(
        columns=[column.model_copy(update={"range": None}) for column in schema.columns]
    ).to_pydantic_model(name)


def find_row_issues(row: Mapping[str, Any], model: type[BaseModel]) -> list[str]:
    """Strict, or pydantic coerces '2' into an int column and passes it."""
    try:
        model.model_validate(
            # pydantic does not know pandas' null forms; this codebase reads them all as absent.
            {
                name: collapse_null_forms(row[name])
                for name in model.model_fields
                if name in row
            },
            strict=True,
        )
    except ValidationError as err:
        return format_errors(err)
    return []


def _find_missing_declared_columns(present: set[str], columns: list[Column]) -> list[Issue]:
    return [
        Issue("error", col.name, f"Missing column '{col.name}'")
        for col in columns
        if col.name and col.name not in present
    ]


# `null_count` alone is not the answer: a float column carries NaN as a VALUE,
# and this codebase reads every null form as absent (see `is_null_form`, which
# the row fingerprint collapses under). A required column holding NaN is missing
# a measurement, so it is counted here.
def _find_nullability_issues(values: pa.ChunkedArray, col: Column) -> list[Issue]:
    if col.nullable:
        return []
    null_n = sum(1 for v in values.to_pylist() if is_null_form(v))
    if null_n > 0:
        return [Issue("error", col.name, f"{null_n} row(s) have no value, but this column is required")]
    return []


def _value_check_for(type_name: str) -> Callable[[Any], bool] | None:
    """None means the type admits any value — not that `type_name` is unrecognised."""
    scalar = CELL_TYPE_PREDICATES.get(type_name)
    if scalar is not None:
        return scalar
    if type_name == JSON_COLUMN_TYPE:
        return None  # `json` is an open object shape — any value goes.
    match = _LIST_TYPE_RE.match(type_name)
    if match:
        element_check = _value_check_for(match.group(1).strip())

        def _is_list_of(v: Any) -> bool:
            if not is_sequence_cell(v):
                return False
            if element_check is None:
                return True
            return all(element_check(x) for x in v if not is_null_form(x))

        return _is_list_of
    return None


def _find_type_issues(values: pa.ChunkedArray, col: Column) -> list[Issue]:
    check = _value_check_for(col.type)
    if check is None:
        return []
    if _is_declared_type_satisfied_by_arrow_type(values.type, col.type):
        return []
    offenders = [v for v in values.to_pylist() if not is_null_form(v) and not check(v)]
    if not offenders:
        return []
    return [
        Issue(
            "error", col.name,
            f"{len(offenders)} value(s) not of declared type '{col.type}' "
            f"(e.g. {_describe_sample(offenders)})",
        )
    ]


# The declared-type vocabulary (`list[X]`, `json`, the scalar set) is app.models
# knowledge app.core may not import, so frames answers about a plain scalar name
# and about "is this arrow type a list, and of what"; the `list[X]` composition,
# which needs both vocabularies, is here.
def _is_declared_type_satisfied_by_arrow_type(arrow_type: pa.DataType, type_name: str) -> bool:
    match = _LIST_TYPE_RE.match(type_name)
    if not match:
        return is_schema_type_satisfied_by_arrow_type(arrow_type, type_name)
    value_type = find_arrow_list_value_type(arrow_type)
    if value_type is None:
        return False
    element_name = match.group(1).strip()
    # `list[json]` admits any element, so being a list at all is the whole check.
    if element_name == JSON_COLUMN_TYPE:
        return True
    return is_schema_type_satisfied_by_arrow_type(value_type, element_name)


def _describe_sample(offenders: list[Any]) -> str:
    shown = ", ".join(repr(v) for v in offenders[:_OFFENDER_SAMPLE_N])
    return shown + ("…" if len(offenders) > _OFFENDER_SAMPLE_N else "")


def _find_numeric_range_issues(values: pa.ChunkedArray, col: Column) -> list[Issue]:
    col_range = col.range
    if not (col_range and col.type in {"int", "float"} and len(col_range) == 2):
        return []
    lo, hi = col_range
    # strings like "+inf" → sentinel; treat as unbounded
    lo_v = -math.inf if (isinstance(lo, str) and RANGE_UNBOUNDED_MARKER in lo) else lo
    hi_v = math.inf if (isinstance(hi, str) and RANGE_UNBOUNDED_MARKER in hi) else hi
    try:
        bad = sum(
            1 for v in values.to_pylist()
            if not is_null_form(v) and (v < lo_v or v > hi_v)
        )
    except TypeError:
        return []  # mixed types — _find_type_issues reports them
    if bad:
        return [Issue("warning", col.name, f"{bad} value(s) outside range [{lo}, {hi}]")]
    return []


def _find_enum_issues(values: pa.ChunkedArray, col: Column) -> list[Issue]:
    if not (col.enum and col.type == STR_COLUMN_TYPE):
        return []
    allowed = set(col.enum)
    offending = [
        str(v) for v in values.to_pylist()
        if not is_null_form(v) and str(v) not in allowed
    ]
    if not offending:
        return []
    distinct = list(dict.fromkeys(offending))
    return [
        Issue(
            "error", col.name,
            f"{len(offending)} value(s) outside enum {sorted(allowed)} "
            f"(e.g. {_describe_sample(distinct)})",
        )
    ]


def _find_undeclared_columns(present: list[str], declared_names: list[str]) -> list[Issue]:
    extras = [c for c in present if c not in declared_names]
    if extras:
        return [
            Issue(
                "warning", None,
                f"{len(extras)} undeclared column(s) present (will be passed through): {extras[:8]}",
            )
        ]
    return []


# A `json`/`list[json]` column MUST declare its shape (`fields` or `value_type`,
# enforced by Column._json_shape) — but nothing checked the data against that
# declaration, so it described the column without constraining it. Arrow types
# the struct precisely, so the whole check reads types and no cells.
def _find_json_shape_issues(values: pa.ChunkedArray, col: Column) -> list[Issue]:
    if col.type not in (JSON_COLUMN_TYPE, LIST_JSON_COLUMN_TYPE):
        return []
    struct = _find_declared_struct(values.type, col.type)
    if struct is None:
        return [Issue("error", col.name, f"'{col.type}' column does not hold objects")]
    present = {struct.field(i).name: struct.field(i).type for i in range(struct.num_fields)}
    if col.value_type is not None:
        return _find_open_map_issues(present, col)
    return _find_declared_field_issues(present, col)


def _find_declared_struct(arrow_type: pa.DataType, type_name: str) -> pa.DataType | None:
    if type_name == LIST_JSON_COLUMN_TYPE:
        element = find_arrow_list_value_type(arrow_type)
        if element is None:
            return None
        arrow_type = element
    return arrow_type if pa.types.is_struct(arrow_type) else None


def _find_declared_field_issues(present: dict[str, pa.DataType], col: Column) -> list[Issue]:
    issues: list[Issue] = []
    for declared in col.fields or []:
        if declared.name not in present:
            issues.append(
                Issue("error", col.name, f"'{col.type}' column is missing field '{declared.name}'")
            )
        elif not _is_declared_type_satisfied_by_arrow_type(
            present[declared.name], declared.type
        ):
            issues.append(Issue(
                "error", col.name,
                f"field '{declared.name}' is {present[declared.name]}, "
                f"not declared type '{declared.type}'",
            ))
    undeclared = sorted(set(present) - {f.name for f in col.fields or []})
    if undeclared:
        issues.append(Issue(
            "warning", col.name,
            f"{len(undeclared)} undeclared field(s) present: {undeclared[:8]}",
        ))
    return issues


def _find_open_map_issues(present: dict[str, pa.DataType], col: Column) -> list[Issue]:
    """An open map declares one value type for every key, so each arrow field must meet it."""
    assert col.value_type is not None  # enforced by Column._json_shape
    offenders = [
        name for name, arrow_type in present.items()
        if not _is_declared_type_satisfied_by_arrow_type(arrow_type, col.value_type)
    ]
    if not offenders:
        return []
    return [Issue(
        "error", col.name,
        f"{len(offenders)} field(s) not of declared value_type '{col.value_type}': "
        f"{sorted(offenders)[:8]}",
    )]
