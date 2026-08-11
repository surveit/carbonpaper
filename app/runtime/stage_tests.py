"""Run a stage's authored tests against its actual code, through the SAME handler
registry the real runner uses. A test is stated in its SIGNATURE's vocabulary: rows
in are what the transform READS, rows out what it WRITES. Comparison is on the
expected columns, counts None and float NaN as one absence, and is a multiset, so
no test pins an ordering.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel

from app.core.frames import list_rows
from app.models import Stage, TableSchema
from app.models.errors import StepRefused
from app.models.stage import StageType
from app.models.stages.signature import transform_input_schemas, transform_output_schema
from app.models.stages.stage_tests import StageTest
from app.runtime.context import RunContext
from app.runtime.stages import HANDLERS
from app.models.severity import UserFacingErrorSeverity
from app.runtime.validation import validate_dataframe

Status = Literal["passed", "mismatch", "error", "malformed"]

# Named handle for the one Status member actually compared below (find_failing_
# stage_tests / _summarize) — the others are only ever constructed, never
# compared, so they don't need a name of their own.
STATUS_PASSED: Status = "passed"


@dataclass
class CellDiff:
    """`row` indexes the compared (sorted) order, not the stage's input rows."""
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
    untested_stages: list[str]

    def count_failing_by_stage(self) -> dict[str, int]:
        counts = {
            run.stage_id: sum(1 for r in run.results if r.status != "passed")
            for run in self.stages
        }
        return {stage_id: n for stage_id, n in counts.items() if n}


def run_stage_tests(
    stages: list[Stage], stage_id: str | None = None
) -> StageTestsReport:
    targets = _select_target_stages(stages, stage_id)
    runs = [_run_one_stage(stage) for stage in targets if stage.tests]
    untested = [stage.id for stage in targets if not stage.tests]
    return StageTestsReport(
        summary=_summarize(runs), stages=runs, untested_stages=untested
    )


def run_tests_for_stage(stage: Stage) -> list[StageTestResult]:
    if not stage.CARRIES_RUNNABLE_TESTS:
        raise ValueError(
            f"stage {stage.id} ({stage.type}) does not carry runnable tests"
        )
    return [_run_one_test(stage, test) for test in (stage.tests or [])]


def find_failing_stage_tests(stages: list[Stage]) -> list[str]:
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
    if stage_id is None:
        return [stage for stage in stages if stage.CARRIES_RUNNABLE_TESTS]
    stage = _find_stage(stages, stage_id)
    if not stage.CARRIES_RUNNABLE_TESTS:
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
    input_schemas = transform_input_schemas(stage)
    input_frames = {
        ref.id: _build_frame(test.inputs[ref.id], input_schemas[ref.id])
        for ref in stage.inputs
    }
    malformed = _validate_test_against_schemas(stage, test, input_frames, input_schemas)
    if malformed:
        return StageTestResult(test.name, "malformed", message=malformed)
    # Ephemeral context: every type declaring CARRIES_RUNNABLE_TESTS runs
    # authored code over its own input and reads neither repo_root/run_dir nor
    # project scope — so both are None (no run on disk), no identity, no cache. A
    # stage reaching for run disk under this context fails loudly via
    # require_run_dir rather than touching a fabricated path.
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
    if isinstance(actual, pd.DataFrame):
        return f"{len(actual)} row(s)"
    return type(actual).__name__


def _judge_raise(test: StageTest, exc: Exception) -> StageTestResult:
    if test.expected is None and isinstance(exc, StepRefused):
        return StageTestResult(test.name, "passed")
    return StageTestResult(test.name, "error", message=f"{type(exc).__name__}: {exc}")


def _build_frame(rows: list[dict[str, Any]], schema: TableSchema) -> pd.DataFrame:
    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=[column.name for column in schema.columns])


def _validate_test_against_schemas(
    stage: Stage,
    test: StageTest,
    input_frames: dict[str, pd.DataFrame],
    input_schemas: dict[str, TableSchema],
) -> str | None:
    problems: list[str] = []
    for ref in stage.inputs:
        report = validate_dataframe(
            input_frames[ref.id], input_schemas[ref.id], stage_id=stage.id, phase="input"
        )
        problems += [
            f"input {ref.id}: {issue.message}"
            for issue in report.issues if issue.severity == UserFacingErrorSeverity.error
        ]
    if test.expected is None:
        # A failure case states no output rows, so there is no output shape to
        # lint — only its inputs, which a real run would still have to accept.
        return "; ".join(problems) if problems else None
    output_schema = transform_output_schema(stage)
    expected_frame = _build_frame(test.expected, output_schema)
    report = validate_dataframe(
        expected_frame, output_schema, stage_id=stage.id, phase="output"
    )
    problems += [
        f"expected rows: {issue.message}"
        for issue in report.issues if issue.severity == UserFacingErrorSeverity.error
    ]
    return "; ".join(problems) if problems else None


def _compare(stage: Stage, test: StageTest, actual: pd.DataFrame) -> StageTestResult:
    output_schema = transform_output_schema(stage)
    assert test.expected is not None  # a failure case was judged before this
    columns = [column.name for column in output_schema.columns]
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
    return {column: row.get(column) for column in columns}


def _values_equal(expected: Any, actual: Any) -> bool:
    """pandas stores a null as NaN or None by column dtype, so the two cannot be told apart."""
    if _is_absent(expected) or _is_absent(actual):
        return _is_absent(expected) and _is_absent(actual)
    return bool(expected == actual)


def _is_absent(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))
