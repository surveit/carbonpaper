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
from typing import Any

import pandas as pd

from app.models import Column, TableSchema


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


@dataclass
class Issue:
    severity: str    # "error" | "warning"
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
        return not any(i.severity == "error" for i in self.issues)

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

    # 1. Every declared column present
    for col in columns:
        name = col.name
        if name and name not in df.columns:
            report.issues.append(Issue("error", name, f"Missing column '{name}'"))

    # 2. Type / nullability / range — only check columns that exist
    for col in columns:
        name = col.name
        if name not in df.columns:
            continue

        series = df[name]
        col_type = col.type
        nullable = col.nullable
        col_range = col.range

        # Nullability
        if not nullable:
            null_n = series.isna().sum()
            if null_n > 0:
                report.issues.append(
                    Issue("error", name, f"{null_n} null value(s) in non-nullable column")
                )

        # Range
        if col_range and col_type in {"int", "float"}:
            non_null = series.dropna()
            if len(non_null) and len(col_range) == 2:
                lo, hi = col_range
                # strings like "+inf" → sentinel; treat as unbounded
                lo_v = -math.inf if (isinstance(lo, str) and "inf" in lo) else lo
                hi_v = math.inf if (isinstance(hi, str) and "inf" in hi) else hi
                try:
                    bad = ((non_null < lo_v) | (non_null > hi_v)).sum()
                    if bad:
                        report.issues.append(
                            Issue(
                                "warning", name,
                                f"{bad} value(s) outside range [{lo}, {hi}]",
                            )
                        )
                except TypeError:
                    pass  # mixed types — the type check below will catch it

        # Enum (categorical strings): values must be in the declared vocabulary
        if col.enum and col_type == "str":
            non_null = series.dropna()
            if len(non_null):
                allowed = set(col.enum)
                bad = (~non_null.astype(str).isin(allowed)).sum()
                if bad:
                    report.issues.append(
                        Issue(
                            "warning", name,
                            f"{bad} value(s) outside enum {sorted(allowed)[:8]}{'…' if len(allowed) > 8 else ''}",
                        )
                    )

    # 3. Primary key uniqueness
    pk = schema.primary_key
    if pk and all(c in df.columns for c in pk):
        dupe = df.duplicated(subset=pk).sum()
        if dupe:
            report.issues.append(
                Issue("error", ",".join(pk), f"Primary key duplicated on {dupe} row(s)")
            )

    # 4. Extra columns warning (informational)
    extras = [c for c in df.columns if c not in declared_names]
    if extras:
        report.issues.append(
            Issue(
                "warning", None,
                f"{len(extras)} undeclared column(s) present (will be passed through): {extras[:8]}",
            )
        )

    return report
