"""Run a python transform's authored tests against its actual code.

A test (app.core.models.stages.stage_tests.StageTest) is a claim about what
given input rows must produce, authored from the methodology. This module holds
the stage's code to those claims: it executes each test through the SAME
handler registry the real runner uses — fidelity comes from sharing the
execution path, not reimplementing it — and reports one StageTestResult each.

Statuses:
  passed    — actual output equals expected under the canonical comparison
  mismatch  — executed cleanly but at least one cell (or the row count) differs
  error     — the stage's function raised; message carries the exception
  malformed — the test itself violates the stage's declared schemas; a bad
              test is its own failure kind, never reported as a code bug

Canonical comparison: cells compare on the output_schema's columns (union of
expected/actual keys when no schema is declared); NaN, None and a missing key
all normalise to null; python_frame_function outputs compare as a multiset of
rows (the type is not order-preserving, so a test cannot pin an ordering)
while order-preserving types compare positionally.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

from app.core.models import Stage, TableSchema
from app.core.models.stage import StageType, is_grain_and_order_preserving
from app.core.models.stages.stage_tests import STAGE_TEST_TYPES, StageTest
from app.runtime.stages import HANDLERS
from app.runtime.validation import validate_dataframe

Status = Literal["passed", "mismatch", "error", "malformed"]


@dataclass
class CellDiff:
    """One differing cell: `row` indexes the canonical (compared) row order."""
    row: int
    column: str
    expected: Any
    actual: Any


@dataclass
class StageTestResult:
    name: str
    status: Status
    diffs: list[CellDiff] = field(default_factory=list)
    message: str | None = None


def run_stage_tests(stage: Stage) -> list[StageTestResult]:
    """Execute each of `stage.tests` through the stage's registered handler
    and compare to its expected rows. Raises ValueError for stage types whose
    tests cannot execute (the model forbids authoring them there anyway)."""
    if stage.type not in STAGE_TEST_TYPES:
        raise ValueError(
            f"stage {stage.id} ({stage.type}) does not carry runnable tests"
        )
    return [_run_one_test(stage, test) for test in (stage.tests or [])]


def find_failing_stage_tests(stages: list[Stage]) -> list[str]:
    """The version gate's check: run every python transform's tests and
    return one human-readable line per test the stage fails ([] = gate open).
    Stages without tests contribute nothing — the gate holds existing tests to
    green; it does not require tests to exist."""
    failures: list[str] = []
    for stage in stages:
        if not stage.tests:
            continue
        for result in run_stage_tests(stage):
            if result.status != "passed":
                detail = result.message or f"{len(result.diffs)} differing cell(s)"
                failures.append(
                    f"stage {stage.id} fails test {result.name!r} ({result.status}) — {detail}"
                )
    return failures


def _run_one_test(stage: Stage, test: StageTest) -> StageTestResult:
    input_frames = {
        ref.id: _build_frame(test.inputs[ref.id], ref.table_schema)
        for ref in stage.inputs
    }
    malformed = _check_test_against_schemas(stage, test, input_frames)
    if malformed:
        return StageTestResult(test.name, "malformed", message=malformed)
    try:
        actual = HANDLERS[StageType(stage.type)].execute(stage, input_frames, ctx={})
    except Exception as exc:  # noqa: BLE001 — the function is authored code; any raise IS the result
        return StageTestResult(
            test.name, "error", message=f"{type(exc).__name__}: {exc}"
        )
    if not isinstance(actual, pd.DataFrame):
        return StageTestResult(
            test.name, "error",
            message=f"function returned {type(actual).__name__}, expected a DataFrame",
        )
    return _compare(stage, test, actual)


def _build_frame(rows: list[dict[str, Any]], schema: TableSchema | None) -> pd.DataFrame:
    """Rows → dataframe. An empty test frame still carries the schema's
    columns, so an "empty input" case validates and executes like a real empty
    upstream output would."""
    if rows:
        return pd.DataFrame(rows)
    columns = [c.name for c in schema.columns] if schema is not None else []
    return pd.DataFrame(columns=columns)


def _check_test_against_schemas(
    stage: Stage, test: StageTest, input_frames: dict[str, pd.DataFrame]
) -> str | None:
    """Schema-lint the test itself (error-severity issues only): its input
    rows against each declared input schema, its expected rows against the
    output schema. Returns a joined message, or None when the test is
    well-formed."""
    problems: list[str] = []
    for ref in stage.inputs:
        report = validate_dataframe(
            input_frames[ref.id], ref.table_schema, stage_id=stage.id, phase="input"
        )
        problems += [
            f"input {ref.id}: {issue.message}"
            for issue in report.issues if issue.severity == "error"
        ]
    expected_frame = _build_frame(test.expected, stage.output_schema)
    report = validate_dataframe(
        expected_frame, stage.output_schema, stage_id=stage.id, phase="output"
    )
    problems += [
        f"expected rows: {issue.message}"
        for issue in report.issues if issue.severity == "error"
    ]
    return "; ".join(problems) if problems else None


def _compare(stage: Stage, test: StageTest, actual: pd.DataFrame) -> StageTestResult:
    columns = _select_comparison_columns(stage, test, actual)
    expected_rows = _canonicalize_rows(test.expected, columns)
    actual_rows = _canonicalize_rows(
        [{str(k): v for k, v in row.items()} for row in actual.to_dict("records")],
        columns,
    )
    if len(expected_rows) != len(actual_rows):
        return StageTestResult(
            test.name, "mismatch",
            message=f"expected {len(expected_rows)} row(s), got {len(actual_rows)}",
        )
    if not is_grain_and_order_preserving(stage.type):
        expected_rows = _sort_canonically(expected_rows)
        actual_rows = _sort_canonically(actual_rows)
    diffs = [
        CellDiff(row=index, column=column,
                 expected=expected_row[column], actual=actual_row[column])
        for index, (expected_row, actual_row) in enumerate(zip(expected_rows, actual_rows))
        for column in columns
        if expected_row[column] != actual_row[column]
    ]
    if diffs:
        return StageTestResult(test.name, "mismatch", diffs=diffs)
    return StageTestResult(test.name, "passed")


def _select_comparison_columns(
    stage: Stage, test: StageTest, actual: pd.DataFrame
) -> list[str]:
    """The columns cells compare on: the output schema's columns when declared
    (undeclared pass-through columns are outside the test's claim), else the
    union of expected keys and actual columns (so an unexpected extra column
    surfaces as a mismatch rather than being silently ignored)."""
    if stage.output_schema is not None:
        return [column.name for column in stage.output_schema.columns]
    seen: dict[str, None] = {}
    for row in test.expected:
        for key in row:
            seen.setdefault(key)
    for key in actual.columns:
        seen.setdefault(str(key))
    return list(seen)


def _canonicalize_rows(
    rows: list[dict[str, Any]], columns: list[str]
) -> list[dict[str, Any]]:
    return [{column: _canonicalize(row.get(column)) for column in columns} for row in rows]


def _canonicalize(value: Any) -> Any:
    """NaN, None and a missing key are the same absence."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass  # non-scalar (list/dict): pd.isna is elementwise there — keep the value
    return value


def _sort_canonically(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A stable, value-based order for multiset comparison of frame outputs."""
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))
