"""Shaping for the examples report and the certification badge, shared by every
host that renders a stage panel. Lives here rather than in app/services because
running an example needs app.runtime, which those services may not import.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

from app.models import WorkflowStage
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
    workflow_stage: Optional[WorkflowStage], test_views: list[dict[str, Any]]
) -> Optional[StageCertification]:
    if workflow_stage is None:
        return None
    stage = workflow_stage.stage
    if stage.find_authored_code_block() is None:
        return None
    if not _summary_of(workflow_stage):
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


def _summary_of(workflow_stage: WorkflowStage) -> Optional[str]:
    block = workflow_stage.stage.find_authored_code_block()
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
    return [
        _shape_one_test(test, result)
        for test, result in zip(tests, results)
    ]


def _shape_one_test(test: StageTest, result: StageTestResult) -> dict[str, Any]:
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
