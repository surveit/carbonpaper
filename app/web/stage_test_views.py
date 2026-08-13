"""Shaping for the examples report and the certification badge, shared by every
host that renders a stage panel. Lives here rather than in app/services because
running an example needs app.runtime, which those services may not import.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

from app.models import TableSchema, WorkflowStage
from app.models.stages.signature import transform_output_schema
from app.models.stages.stage_tests import StageTest
from app.runtime.stage_tests import STATUS_PASSED, StageTestResult, run_tests_for_stage

CertificationStatus = Literal[
    "certified", "failing", "untested", "unsummarised", "untestable"
]

# Which of the panel's three sections a case belongs in. `from_data` is the step running
# on rows a run really produced; the other two are about data that has not arrived, split
# by what the step would do with it.
TestSection = Literal["from_data", "reject_and_defer", "decide_now"]


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
        _shape_one_test(test, result, workflow_stage)
        for test, result in zip(tests, results)
    ]


def _shape_one_test(
    test: StageTest, result: StageTestResult, workflow_stage: WorkflowStage
) -> dict[str, Any]:
    input_schemas = {ref.id: ref.table_schema for ref in workflow_stage.inputs}
    return {
        "name": test.name,
        "description": test.description,
        "status": result.status,
        "message": result.message,
        # What the step DID, which `status` does not imply: a passed case may have
        # returned a table or refused, and the panel leads with which.
        "outcome": result.outcome,
        "outcome_detail": result.outcome_detail,
        "returned": _shape_returned(result, workflow_stage),
        "inputs": [
            {"stage_id": stage_id,
             "columns": _order_columns(rows, input_schemas.get(stage_id)),
             "rows": rows,
             "selections": _shape_selections(test, stage_id)}
            for stage_id, rows in test.inputs.items()
        ],
        "section": _name_the_section(test),
        # Why an input like this could turn up later. Only a written case carries one —
        # a selected row already happened, so there is nothing to anticipate.
        "authored_reason": test.authored_reason,
        # None, not an empty table: a failure case claims the step must fail, which
        # the template must not render as "succeeded, returned nothing".
        "expected": None if test.expected is None else _shape_expected(
            test.expected, workflow_stage
        ),
        "diffs": [
            {"row": diff.row, "column": diff.column,
             "expected": diff.expected, "actual": diff.actual}
            for diff in result.diffs
        ],
    }


def _name_the_section(test: StageTest) -> TestSection:
    """A case is about the data as it stands, or about data that has not arrived."""
    if test.selections:
        return "from_data"
    # No row to select, so the case turns on what the step does when one appears:
    # stop and leave the decision open, or act on what the description already says.
    return "reject_and_defer" if test.expected is None else "decide_now"


def _shape_selections(test: StageTest, stage_id: str) -> list[dict[str, Any]]:
    """One entry per row of this input, in the order the rows stand in the table."""
    return [
        {"run_id": selection.run_id, "row": selection.row, "filter": selection.filter,
         "matched": selection.matched, "scanned": selection.scanned}
        for selection in test.selections if selection.input == stage_id
    ]


def _shape_returned(
    result: StageTestResult, workflow_stage: WorkflowStage
) -> dict[str, Any]:
    columns = _order_columns(result.returned_rows, workflow_stage.output_schema)
    # Declared order and the same shading as the expected table: the reader compares
    # the two, so a carried-through column must stand in the same place in both.
    return {
        "columns": columns or result.returned_columns,
        "rows": result.returned_rows,
        "total": result.returned_total,
        "written_columns": _find_written_columns(workflow_stage, columns),
    }


def _shape_expected(
    rows: list[dict[str, Any]], workflow_stage: WorkflowStage
) -> dict[str, Any]:
    columns = _order_columns(rows, workflow_stage.output_schema)
    return {
        "columns": columns,
        "rows": rows,
        "written_columns": _find_written_columns(workflow_stage, columns),
    }


def _order_columns(
    rows: list[dict[str, Any]], schema: Optional[TableSchema]
) -> list[str]:
    # Declared order, so a carried-through column stands in the same place in both tables.
    stated = _list_row_columns(rows)
    declared = [column.name for column in schema.columns] if schema else []
    return [name for name in declared if name in stated] + [
        # A key the schema does not name still shows, after the ones it does.
        name for name in stated if name not in set(declared)
    ]


def _find_written_columns(
    workflow_stage: WorkflowStage, columns: list[str]
) -> list[str]:
    schema = transform_output_schema(workflow_stage.stage)
    written = {column.name for column in schema.columns}
    marked = [name for name in columns if name in written]
    # A mark every column carries separates nothing — a replaces-form step writes the
    # whole row, so there is no carried-through column to tell it from.
    return [] if len(marked) == len(columns) else marked


def _list_row_columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key)
    return list(seen)
