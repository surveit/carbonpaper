"""Shaping for the examples report and the certification badge, shared by every
host that renders a stage panel. Lives here rather than in app/services because
running an example needs app.runtime, which those services may not import.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

from app.models import Stage, WorkflowStage
from app.models.stages.stage_tests import StageTest
from app.runtime.stage_tests import STATUS_PASSED, StageTestResult, run_tests_for_stage

CertificationStatus = Literal[
    "certified", "failing", "untested", "unsummarised", "untestable"
]


class StageCertification(BaseModel):
    """`certified` means the summary and the code agree ON THE AUTHORED EXAMPLES, not that it is right."""

    status: CertificationStatus
    passing: int = 0
    total: int = 0

    @property
    def is_certified(self) -> bool:
        return self.status == "certified"


def build_certification(
    stage: Stage, test_views: list[dict[str, Any]]
) -> Optional[StageCertification]:
    if not _carries_authored_code(stage):
        return None
    if not _summary_of(stage):
        return StageCertification(status="unsummarised", total=len(test_views))
    if not stage.CARRIES_RUNNABLE_TESTS:
        return StageCertification(status="untestable")
    if not test_views:
        return StageCertification(status="untested")
    passing = sum(1 for view in test_views if view["status"] == STATUS_PASSED)
    return StageCertification(
        status="certified" if passing == len(test_views) else "failing",
        passing=passing,
        total=len(test_views),
    )


def _carries_authored_code(stage: Stage) -> bool:
    return stage.find_authored_code_block() is not None


def _summary_of(stage: Stage) -> Optional[str]:
    block = stage.find_authored_code_block()
    return block.summary if block is not None else None


def shape_test_views(
    workflow_stage: Optional[WorkflowStage]
) -> list[dict[str, Any]]:
    if workflow_stage is None:
        return []
    tests = workflow_stage.stage.tests
    if not tests:
        return []
    results = run_tests_for_stage(workflow_stage.stage)
    new_columns = _find_new_output_columns(workflow_stage)
    return [
        _shape_one_test(test, result, new_columns)
        for test, result in zip(tests, results)
    ]


def _find_new_output_columns(workflow_stage: WorkflowStage) -> list[str]:
    # Off the declared schemas, not the example rows: an empty input would read as
    # adding every column.
    upstream = {
        column.name
        for stage_input in workflow_stage.inputs
        for column in stage_input.table_schema.columns
    }
    output_schema = workflow_stage.output_schema
    declared = output_schema.columns if output_schema else []
    return [column.name for column in declared if column.name not in upstream]


def _shape_one_test(
    test: StageTest, result: StageTestResult, new_columns: list[str]
) -> dict[str, Any]:
    return {
        "name": test.name,
        "description": test.description,
        "status": result.status,
        "message": result.message,
        "inputs": [
            {"stage_id": stage_id, "columns": _list_row_columns(rows), "rows": rows}
            for stage_id, rows in test.inputs.items()
        ],
        # None, not an empty table: a failure case claims the step must fail, which
        # the template must not render as "succeeded, returned nothing".
        "expected": None if test.expected is None else {
            "columns": _list_row_columns(test.expected), "rows": test.expected,
            "new_columns": new_columns,
        },
        "diffs": [
            {"row": diff.row, "column": diff.column,
             "expected": diff.expected, "actual": diff.actual}
            for diff in result.diffs
        ],
    }


def _list_row_columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key)
    return list(seen)
