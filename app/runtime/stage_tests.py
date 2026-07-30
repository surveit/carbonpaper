"""Run a python transform's authored tests against its actual code, executing each
through the SAME handler registry the real runner uses.

Comparison is on the output_schema's columns, counts None and float NaN as one
absence, and compares both sides as a multiset, so no test pins an ordering.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel

from app.core.errors import StepRefused
from app.core.frames import list_rows
from app.models import Stage, TableSchema
from app.models.stage import StageType
from app.models.stages.stage_tests import STAGE_TEST_TYPES, StageTest
from app.runtime.context import RunContext
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


# ─── Typed run report (JSON-serialisable; the MCP run_stage_tests tool returns this) ─
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


class StageTestsReport(BaseModel):
    summary: TestRunSummary
    stages: list[StageTestRun]
    untested_python_stages: list[str]


def run_stage_tests(
    stages: list[Stage], stage_id: str | None = None
) -> StageTestsReport:
    """Run authored stage tests against their current code and report the result.

    With `stage_id` None, run every python-transform stage that carries tests;
    with a `stage_id`, run just that stage (raising ValueError if it names no
    stage, or names one whose type carries no runnable tests). Either way,
    `untested_python_stages` lists the in-scope python transforms that have no
    tests — a coverage gap the caller should see, not a failure."""
    targets = _select_target_stages(stages, stage_id)
    runs = [_run_one_stage(stage) for stage in targets if stage.tests]
    untested = [stage.id for stage in targets if not stage.tests]
    return StageTestsReport(
        summary=_summarize(runs), stages=runs, untested_python_stages=untested
    )


def run_tests_for_stage(stage: Stage) -> list[StageTestResult]:
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
        for result in run_tests_for_stage(stage):
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
        stage_id=stage.id, stage_type=stage.type, results=run_tests_for_stage(stage)
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
    # Ephemeral context: authored tests run only python_row_function /
    # python_frame_function (STAGE_TEST_TYPES), neither of which reads
    # repo_root/run_dir or needs project scope — so both are None (no run on
    # disk), no identity, no cache. A stage reaching for run disk under this
    # context fails loudly via require_run_dir rather than touching a fabricated
    # path.
    ctx = RunContext.for_stages_outside_a_run(None, None)
    try:
        actual = HANDLERS[StageType(stage.type)].execute(stage, input_frames, ctx)
    except Exception as exc:  # noqa: BLE001 — the function is authored code; any raise IS the result
        return _judge_raise(test, exc)
    if test.expected is None:
        return StageTestResult(
            test.name, "mismatch",
            message=f"expected the step to fail, got {_describe_output(actual)}",
        )
    if not isinstance(actual, pd.DataFrame):
        return StageTestResult(
            test.name, "error",
            message=f"function returned {type(actual).__name__}, expected a DataFrame",
        )
    return _compare(stage, test, actual)


def _describe_output(actual: Any) -> str:
    """What the step returned instead of failing — row count only if it is a frame."""
    if isinstance(actual, pd.DataFrame):
        return f"{len(actual)} row(s)"
    return type(actual).__name__


def _judge_raise(test: StageTest, exc: Exception) -> StageTestResult:
    """Only StepRefused satisfies a failure case; every other raise is an error.

    The type carries the whole signal — nothing is matched against the message. A
    step that refuses says so by raising StepRefused; a KeyError from the same input
    is the step falling over, which is what the test was written to tell apart, and
    it stays an error even on a test that expected a failure."""
    if test.expected is None and isinstance(exc, StepRefused):
        return StageTestResult(test.name, "passed")
    return StageTestResult(test.name, "error", message=f"{type(exc).__name__}: {exc}")


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
    if test.expected is None:
        # A failure case states no output rows, so there is no output shape to
        # lint — only its inputs, which a real run would still have to accept.
        return "; ".join(problems) if problems else None
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
    # schema-less terminal stage) cannot carry tests. And a failure case
    # (expected is None) has already been judged by the time we compare rows.
    assert stage.output_schema is not None
    assert test.expected is not None
    columns = [column.name for column in stage.output_schema.columns]
    expected_rows = [_select_cells(row, columns) for row in test.expected]
    actual_rows = [_select_cells(row, columns) for row in list_rows(actual)]
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
    """Cell equality treating null and NaN as one absence: two absent cells are
    equal, an absent and a present cell are not, and two present cells compare
    by `==`. A pandas frame stores a null as NaN in a numeric column and as None
    in an object column, so the runtime cannot tell the two apart — a test that
    expects a null therefore matches either."""
    if _is_absent(expected) or _is_absent(actual):
        return _is_absent(expected) and _is_absent(actual)
    return bool(expected == actual)


def _is_absent(value: Any) -> bool:
    """A cell is absent when it is None or float NaN — the two forms a null
    takes in a pandas frame, which do not survive as distinct values."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A stable, value-based row order applied to both sides so they compare
    as a multiset — no test pins an output ordering."""
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))
