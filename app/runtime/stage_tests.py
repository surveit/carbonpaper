"""Run a python transform's authored tests against its actual code.

A test (app.core.models.stages.stage_tests.StageTest) is a claim about what
given input rows must produce, authored from the methodology. This module holds
the stage's code to those claims: it executes each test through the SAME
handler registry the real runner uses — fidelity comes from sharing the
execution path, not reimplementing it — and reports one StageTestResult each.

Statuses:
  passed    — actual output equals expected under the comparison below
  mismatch  — executed cleanly but at least one cell (or the row count) differs
  error     — the stage's function raised; message carries the exception
  malformed — the test itself violates the stage's declared schemas; a bad
              test is its own failure kind, never reported as a code bug

Comparison: cells compare on the output_schema's columns. None and NaN are
distinct values — a cell matches iff the values are equal, where None equals
only None and float NaN equals float NaN (a plain `==` would make NaN unequal
to itself). An omitted column in a row is a claim of None. Both sides compare
as a multiset of rows: each is sorted into the same value-based order first,
so no test pins an ordering (a python_row_function test is one row in → one
row out, where order is vacuous; python_frame_function is not
order-preserving).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel

from app.core.models import Stage, TableSchema
from app.core.models.stage import StageType
from app.core.models.stages.stage_tests import STAGE_TEST_TYPES, StageTest
from app.runtime.stages import HANDLERS
from app.runtime.validation import Severity, validate_dataframe

Status = Literal["passed", "mismatch", "error", "malformed"]

# Named handle for the one Status member actually compared below (find_failing_
# stage_tests / _summarize) — the others are only ever constructed, never
# compared, so they don't need a name of their own.
STATUS_PASSED: Status = "passed"


@dataclass
class CellDiff:
    """One differing cell: `row` indexes the compared (sorted) row order."""
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


# ─── Typed run report (JSON-serialisable; the MCP run_tests tool returns this) ─
# StageTestResult is one test's outcome; these models aggregate a run of them
# into a report a caller (an authoring agent) reads. StageTestRun embeds the
# StageTestResult dataclasses directly — Pydantic serialises them under
# model_dump(mode="json") — so there is one result type, not a mirror of it.

class StageTestRun(BaseModel):
    stage_id: str
    stage_type: str
    results: list[StageTestResult]


class TestRunSummary(BaseModel):
    stages_run: int
    tests_total: int
    passed: int
    failed: int


class WorkflowTestReport(BaseModel):
    summary: TestRunSummary
    stages: list[StageTestRun]
    untested_python_stages: list[str]


def run_workflow_tests(
    stages: list[Stage], stage_id: str | None = None
) -> WorkflowTestReport:
    """Run authored stage tests against their current code and report the result.

    With `stage_id` None, run every python-transform stage that carries tests;
    with a `stage_id`, run just that stage (raising ValueError if it names no
    stage, or names one whose type carries no runnable tests). Either way,
    `untested_python_stages` lists the in-scope python transforms that have no
    tests — a coverage gap the caller should see, not a failure."""
    targets = _select_target_stages(stages, stage_id)
    runs = [_run_one_stage(stage) for stage in targets if stage.tests]
    untested = [stage.id for stage in targets if not stage.tests]
    return WorkflowTestReport(
        summary=_summarize(runs), stages=runs, untested_python_stages=untested
    )


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
            if result.status != STATUS_PASSED:
                detail = result.message or f"{len(result.diffs)} differing cell(s)"
                failures.append(
                    f"stage {stage.id} fails test {result.name!r} ({result.status}) — {detail}"
                )
    return failures


def _select_target_stages(stages: list[Stage], stage_id: str | None) -> list[Stage]:
    """The python-transform stages a run covers: all of them when `stage_id` is
    None, or exactly the named one — raising ValueError if it is absent or is not
    a stage type that carries runnable tests."""
    if stage_id is None:
        return [stage for stage in stages if stage.type in STAGE_TEST_TYPES]
    stage = _find_stage(stages, stage_id)
    if stage.type not in STAGE_TEST_TYPES:
        raise ValueError(
            f"stage {stage_id} ({stage.type}) does not carry runnable tests"
        )
    return [stage]


def _find_stage(stages: list[Stage], stage_id: str) -> Stage:
    for stage in stages:
        if stage.id == stage_id:
            return stage
    raise ValueError(f"no stage '{stage_id}' in the workflow")


def _run_one_stage(stage: Stage) -> StageTestRun:
    return StageTestRun(
        stage_id=stage.id, stage_type=stage.type, results=run_stage_tests(stage)
    )


def _summarize(runs: list[StageTestRun]) -> TestRunSummary:
    results = [result for run in runs for result in run.results]
    passed = sum(1 for result in results if result.status == STATUS_PASSED)
    return TestRunSummary(
        stages_run=len(runs),
        tests_total=len(results),
        passed=passed,
        failed=len(results) - passed,
    )


def _run_one_test(stage: Stage, test: StageTest) -> StageTestResult:
    input_frames = {
        ref.id: _build_frame(test.inputs[ref.id], ref.table_schema)
        for ref in stage.inputs
    }
    malformed = _validate_test_against_schemas(stage, test, input_frames)
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


def _validate_test_against_schemas(
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
            for issue in report.issues if issue.severity == Severity.error
        ]
    expected_frame = _build_frame(test.expected, stage.output_schema)
    report = validate_dataframe(
        expected_frame, stage.output_schema, stage_id=stage.id, phase="output"
    )
    problems += [
        f"expected rows: {issue.message}"
        for issue in report.issues if issue.severity == Severity.error
    ]
    return "; ".join(problems) if problems else None


def _compare(stage: Stage, test: StageTest, actual: pd.DataFrame) -> StageTestResult:
    # Python transforms always declare their output schema; publish (the
    # schema-less terminal stage) cannot carry tests.
    assert stage.output_schema is not None
    columns = [column.name for column in stage.output_schema.columns]
    expected_rows = [_select_cells(row, columns) for row in test.expected]
    actual_rows = [
        _select_cells({str(key): value for key, value in row.items()}, columns)
        for row in actual.to_dict("records")
    ]
    if len(expected_rows) != len(actual_rows):
        return StageTestResult(
            test.name, "mismatch",
            message=f"expected {len(expected_rows)} row(s), got {len(actual_rows)}",
        )
    expected_rows = _sort_rows(expected_rows)
    actual_rows = _sort_rows(actual_rows)
    diffs = [
        CellDiff(row=index, column=column,
                 expected=expected_row[column], actual=actual_row[column])
        for index, (expected_row, actual_row) in enumerate(zip(expected_rows, actual_rows))
        for column in columns
        if not _values_equal(expected_row[column], actual_row[column])
    ]
    if diffs:
        return StageTestResult(test.name, "mismatch", diffs=diffs)
    return StageTestResult(test.name, "passed")


def _select_cells(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    """Project a row onto the comparison columns. An omitted column in a row is
    a claim of None: the malformed gate only guarantees each declared column
    appears somewhere in the expected rows, not in every row dict."""
    return {column: row.get(column) for column in columns}


def _values_equal(expected: Any, actual: Any) -> bool:
    """Cell equality with None and NaN as distinct values: None equals only
    None, float NaN equals float NaN (a plain `==` would make NaN unequal to
    itself), anything else compares by `==`."""
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, float) and isinstance(actual, float):
        if math.isnan(expected) or math.isnan(actual):
            return math.isnan(expected) and math.isnan(actual)
    return bool(expected == actual)


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A stable, value-based row order applied to both sides so they compare
    as a multiset — no test pins an output ordering."""
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))
