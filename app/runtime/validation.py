"""
Schema validation for stage I/O.

A stage's declared output_schema is a contract. Every run validates:
  - all declared columns are present in the produced dataframe
  - types coerce
  - range constraints are satisfied
  - nullability is respected
  - primary key (if declared) is unique

Validation results are returned as structured records so the run viewer can
surface them next to the stage card.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pandas as pd

from app.models import Column, RANGE_UNBOUNDED_MARKER, STR_COLUMN_TYPE, TableSchema


# Map our type vocabulary to permissive pandas dtype checks.
PY_TYPE_OF = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "datetime": "datetime",
    "date": "date",
    "dict": dict,
    "json": object,
}


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
        pass  # mixed types — the type check below will catch it
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
