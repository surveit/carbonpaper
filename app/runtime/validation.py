"""Schema validation for stage I/O: column presence, type coercion, range
constraints, nullability, and primary-key uniqueness against a stage's declared
output_schema. Results are returned as structured records, not raised.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from app.models import (
    Column,
    JSON_COLUMN_TYPE,
    RANGE_UNBOUNDED_MARKER,
    SCALAR_COLUMN_TYPES,
    STR_COLUMN_TYPE,
    TableSchema,
)


# ── Type checking ────────────────────────────────────────────────────────────
# One predicate per scalar name in app.models.SCALAR_COLUMN_TYPES, answering
# "may this Python value sit in a column declared as T?". Deliberately
# permissive where pandas is lossy (numpy scalars, int-valued floats), and
# deliberately strict where the distinction is real (a bool is not an int).
_LIST_TYPE_RE = re.compile(r"^list\[(.+)\]$")


def _is_bool(v: Any) -> bool:
    return isinstance(v, (bool, np.bool_))


def _is_int(v: Any) -> bool:
    # A Python bool is a subclass of int, but a column declared `int` that
    # holds True/False is a real mismatch — reject it explicitly.
    if _is_bool(v):
        return False
    if isinstance(v, (int, np.integer)):
        return True
    # An int column carrying a null is promoted to float64 by pandas, so a
    # whole-valued float is an int that survived a lossy round-trip, not a
    # type error. 1.5 in an int column still is one.
    return isinstance(v, (float, np.floating)) and float(v).is_integer()


def _is_float(v: Any) -> bool:
    # Ints in a float column are fine (pandas will not preserve the
    # distinction anyway); bools are not.
    return isinstance(v, (float, np.floating)) or _is_int(v)


def _is_str(v: Any) -> bool:
    # np.str_ subclasses str; pandas `string` dtype yields plain str.
    return isinstance(v, str)


def _is_datetime(v: Any) -> bool:
    # datetime.datetime covers pd.Timestamp (a subclass of it).
    return isinstance(v, (_dt.datetime, np.datetime64))


def _is_date(v: Any) -> bool:
    # datetime.date covers datetime.datetime and pd.Timestamp: pandas has no
    # date-only dtype, so a `date` column round-trips as a Timestamp.
    return isinstance(v, (_dt.date, np.datetime64))


_SCALAR_VALUE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "str": _is_str,
    "int": _is_int,
    "float": _is_float,
    "bool": _is_bool,
    "datetime": _is_datetime,
    "date": _is_date,
}

# Keep the vocabulary honest: every scalar the models accept must have a check.
assert set(_SCALAR_VALUE_CHECKS) == SCALAR_COLUMN_TYPES, (
    "app.runtime.validation type checks drifted from "
    f"SCALAR_COLUMN_TYPES: {SCALAR_COLUMN_TYPES ^ set(_SCALAR_VALUE_CHECKS)}"
)

# How many offending values to name in an Issue message.
_TYPE_SAMPLE_N = 3


class Severity(str, Enum):
    """An `Issue`'s severity — `error` fails `ValidationReport.ok`, `warning`
    is informational only."""
    error = "error"
    warning = "warning"


@dataclass
class Issue:
    severity: str    # Severity.error | Severity.warning
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
        return not any(i.severity == Severity.error for i in self.issues)

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
        report.issues.append(Issue("warning", None, "No schema declared; skipping checks."))
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

    report.issues.extend(_find_duplicate_primary_keys(df, schema.primary_key))
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
        return [Issue("error", col.name, f"{null_n} null value(s) in non-nullable column")]
    return []


def _value_check_for(type_name: str) -> Callable[[Any], bool] | None:
    """The predicate a value must satisfy for a column declared `type_name`, or
    None when the type admits anything (`json`, or a type we have no opinion
    on)."""
    scalar = _SCALAR_VALUE_CHECKS.get(type_name)
    if scalar is not None:
        return scalar
    if type_name == JSON_COLUMN_TYPE:
        return None  # `json` is an open object shape — any value goes.
    match = _LIST_TYPE_RE.match(type_name)
    if match:
        element_check = _value_check_for(match.group(1).strip())

        def _is_list_of(v: Any) -> bool:
            if not isinstance(v, (list, tuple, np.ndarray)):
                return False
            if element_check is None:
                return True
            return all(element_check(x) for x in v if not _is_null(x))

        return _is_list_of
    return None


def _is_null(v: Any) -> bool:
    """Scalar-safe null test — `pd.isna` returns an array for list values."""
    if v is None:
        return True
    if isinstance(v, (list, tuple, np.ndarray, dict)):
        return False
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _dtype_satisfies(series: pd.Series, type_name: str) -> bool:
    """Whether the series' dtype alone proves every value conforms — the fast
    path that skips per-value inspection. Covers numpy dtypes and the pandas
    nullable extension dtypes (`boolean`, `Int64`, `Float64`, `string`)."""
    dtype = series.dtype
    types = pd.api.types
    if types.is_object_dtype(dtype):
        return False  # the interesting case: values must be inspected
    if type_name == "bool":
        return bool(types.is_bool_dtype(dtype))
    if type_name == "int":
        # A float dtype may be an int column that met a null — not provable
        # from the dtype, so fall through to the per-value check, which accepts
        # whole-valued floats (see _is_int).
        return bool(types.is_integer_dtype(dtype) and not types.is_bool_dtype(dtype))
    if type_name == "float":
        return bool(
            (types.is_float_dtype(dtype) or types.is_integer_dtype(dtype))
            and not types.is_bool_dtype(dtype)
        )
    if type_name == STR_COLUMN_TYPE:
        return isinstance(dtype, pd.StringDtype)
    if type_name in ("datetime", "date"):
        return bool(types.is_datetime64_any_dtype(dtype))
    return False


def _find_type_issues(series: pd.Series, col: Column) -> list[Issue]:
    """Values that do not match the column's declared type. Nulls are skipped —
    reporting them is `_find_nullability_issues`' job."""
    check = _value_check_for(col.type)
    if check is None:
        return []
    if _dtype_satisfies(series, col.type):
        return []
    non_null = series[~series.map(_is_null)]
    if not len(non_null):
        return []
    offenders = [v for v in non_null if not check(v)]
    if not offenders:
        return []
    sample = ", ".join(repr(v) for v in offenders[:_TYPE_SAMPLE_N])
    ellipsis = "…" if len(offenders) > _TYPE_SAMPLE_N else ""
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
    bad = (~non_null.astype(str).isin(allowed)).sum()
    if bad:
        return [
            Issue(
                "warning", col.name,
                f"{bad} value(s) outside enum {sorted(allowed)[:8]}{'…' if len(allowed) > 8 else ''}",
            )
        ]
    return []


def _find_duplicate_primary_keys(df: pd.DataFrame, pk: list[str] | None) -> list[Issue]:
    if not (pk and all(c in df.columns for c in pk)):
        return []
    dupe = df.duplicated(subset=pk).sum()
    if dupe:
        return [Issue("error", ",".join(pk), f"Primary key duplicated on {dupe} row(s)")]
    return []


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
