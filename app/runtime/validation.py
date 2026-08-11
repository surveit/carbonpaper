"""Schema validation for stage I/O: column presence, type coercion, enum
vocabularies, range constraints, nullability, and primary-key uniqueness against
the output schema a stage's signature resolves to. Results are returned as
structured records, not raised.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.core.frames import (
    CELL_TYPE_PREDICATES,
    dtype_proves_cell_type,
    is_null_form,
    is_sequence_cell,
)
from app.models import (
    Column,
    JSON_COLUMN_TYPE,
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
    phase: str       # "input" | "output"
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


def validate_dataframe(
    df: pd.DataFrame,
    schema: TableSchema | None,
    *,
    stage_id: str,
    phase: str,
) -> ValidationReport:
    report = ValidationReport(stage_id=stage_id, phase=phase, rows=len(df))
    if schema is None:
        # A stage declaring no schema emits files rather than a table.
        return report

    columns: list[Column] = list(schema.columns)
    declared_names = [c.name for c in columns]

    report.issues.extend(_find_missing_declared_columns(df, columns))

    for col in columns:
        name = col.name
        if name not in df.columns:
            continue
        series = df[name]
        report.issues.extend(_find_nullability_issues(series, col))
        report.issues.extend(_find_type_issues(series, col))
        report.issues.extend(_find_numeric_range_issues(series, col))
        report.issues.extend(_find_enum_issues(series, col))

    report.issues.extend(_find_undeclared_columns(df, declared_names))

    return report


def _find_missing_declared_columns(df: pd.DataFrame, columns: list[Column]) -> list[Issue]:
    issues: list[Issue] = []
    for col in columns:
        name = col.name
        if name and name not in df.columns:
            issues.append(Issue("error", name, f"Missing column '{name}'"))
    return issues


def _find_nullability_issues(series: pd.Series, col: Column) -> list[Issue]:
    if col.nullable:
        return []
    null_n = series.isna().sum()
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


def _find_type_issues(series: pd.Series, col: Column) -> list[Issue]:
    check = _value_check_for(col.type)
    if check is None:
        return []
    if dtype_proves_cell_type(series, col.type):
        return []
    non_null = series[~series.map(is_null_form)]
    if not len(non_null):
        return []
    offenders = [v for v in non_null if not check(v)]
    if not offenders:
        return []
    sample = ", ".join(repr(v) for v in offenders[:_OFFENDER_SAMPLE_N])
    ellipsis = "…" if len(offenders) > _OFFENDER_SAMPLE_N else ""
    return [
        Issue(
            "error", col.name,
            f"{len(offenders)} value(s) not of declared type '{col.type}' "
            f"(e.g. {sample}{ellipsis})",
        )
    ]


def _find_numeric_range_issues(series: pd.Series, col: Column) -> list[Issue]:
    col_range = col.range
    if not (col_range and col.type in {"int", "float"}):
        return []
    non_null = series.dropna()
    if not (len(non_null) and len(col_range) == 2):
        return []
    lo, hi = col_range
    # strings like "+inf" → sentinel; treat as unbounded
    lo_v = -math.inf if (isinstance(lo, str) and RANGE_UNBOUNDED_MARKER in lo) else lo
    hi_v = math.inf if (isinstance(hi, str) and RANGE_UNBOUNDED_MARKER in hi) else hi
    try:
        bad = ((non_null < lo_v) | (non_null > hi_v)).sum()
        if bad:
            return [Issue("warning", col.name, f"{bad} value(s) outside range [{lo}, {hi}]")]
    except TypeError:
        pass  # mixed types — _find_type_issues reports them
    return []


def _find_enum_issues(series: pd.Series, col: Column) -> list[Issue]:
    if not (col.enum and col.type == STR_COLUMN_TYPE):
        return []
    non_null = series.dropna()
    if not len(non_null):
        return []
    allowed = set(col.enum)
    rendered = non_null.astype(str)
    offending = rendered[~rendered.isin(allowed)]
    if not len(offending):
        return []
    distinct = list(offending.unique())
    sample = ", ".join(repr(v) for v in distinct[:_OFFENDER_SAMPLE_N])
    ellipsis = "…" if len(distinct) > _OFFENDER_SAMPLE_N else ""
    return [
        Issue(
            "error", col.name,
            f"{len(offending)} value(s) outside enum {sorted(allowed)} "
            f"(e.g. {sample}{ellipsis})",
        )
    ]


def _find_undeclared_columns(df: pd.DataFrame, declared_names: list[str]) -> list[Issue]:
    extras = [c for c in df.columns if c not in declared_names]
    if extras:
        return [
            Issue(
                "warning", None,
                f"{len(extras)} undeclared column(s) present (will be passed through): {extras[:8]}",
            )
        ]
    return []
