"""Shaping for the test-report and certification surfaces, shared by every host
that renders a stage panel (node review, run detail, version detail, lineage).
Lives here rather than in app/services because running a test needs app.runtime,
which the services those hosts sit on may not import.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

from app.models import Stage
from app.models.stages.stage_tests import STAGE_TEST_TYPES, StageTest
from app.runtime.stage_tests import STATUS_PASSED, StageTestResult, run_tests_for_stage

CertificationStatus = Literal[
    "certified", "failing", "untested", "unsummarised", "untestable", "n/a"
]


class StageCertification(BaseModel):
    """Whether a stage's plain-language summary has been checked against its code,
    and on how many cases.

    The claim a `certified` status licenses is narrow and the template says so out
    loud: the tests were authored from the methodology, so passing means the
    summary and the code agree ON THESE CASES. It is not a proof of correctness,
    and it says nothing about inputs no case covers."""

    status: CertificationStatus
    passing: int = 0
    total: int = 0

    @property
    def is_certified(self) -> bool:
        return self.status == "certified"


def build_certification(
    stage: Stage, test_views: list[dict[str, Any]]
) -> StageCertification:
    """The certification state of `stage` given its already-run `test_views`.

    Ordered so that MISSING A DESCRIPTION outranks being untestable: a stage whose
    behaviour is authored code is unreviewable without a description whether or not
    an example could ever certify one, and answering `n/a` there would drop it from
    every surface. `n/a` is only for a stage carrying no authored code at all — its
    behaviour is config a reviewer reads directly (a join's keys, a union's inputs),
    with no prose standing between them and it."""
    if not _carries_authored_code(stage):
        return StageCertification(status="n/a")
    if not _summary_of(stage):
        return StageCertification(status="unsummarised", total=len(test_views))
    if stage.type not in STAGE_TEST_TYPES:
        return StageCertification(status="untestable")
    if not test_views:
        return StageCertification(status="untested")
    passing = sum(1 for view in test_views if view["status"] == STATUS_PASSED)
    return StageCertification(
        status="certified" if passing == len(test_views) else "failing",
        passing=passing,
        total=len(test_views),
    )


IssueKind = Literal["contradicted", "undescribed", "untested", "untestable"]

# Worst first: the order the Workflow page lists them in, and the order that
# decides the roll-up's own severity.
_ISSUE_SEVERITY: dict[str, str] = {
    "contradicted": "error",
    "undescribed": "warning",
    "untested": "warning",
    "untestable": "note",
}


class WorkflowIssue(BaseModel):
    """One reason a step is not reviewable from its description. Raised per stage,
    not per failing example — a reviewer acts on the step, not on the case."""

    kind: IssueKind
    stage_id: str
    stage_name: str
    detail: str

    @property
    def severity(self) -> str:
        return _ISSUE_SEVERITY[self.kind]


def build_workflow_issues(stages: list[Stage]) -> list[WorkflowIssue]:
    """Every step whose description a reviewer cannot rely on, worst first.

    A certified stage raises nothing — this is a problem list, not a scoreboard.
    Runs each stage's examples, so it costs what the examples cost: cheap in
    practice, and only for stages that carry any."""
    issues: list[WorkflowIssue] = []
    for stage in stages:
        views = shape_test_views(stage)
        certification = build_certification(stage, views)
        match certification.status:
            case "failing":
                failing = [v["name"] for v in views if v["status"] != STATUS_PASSED]
                issues.append(WorkflowIssue(
                    kind="contradicted", stage_id=stage.id, stage_name=stage.name,
                    detail=(
                        f"{len(failing)} of {certification.total} examples disagree with "
                        f"the description: {', '.join(failing)}"
                    ),
                ))
            case "unsummarised":
                issues.append(WorkflowIssue(
                    kind="undescribed", stage_id=stage.id, stage_name=stage.name,
                    detail="No plain-language description — reviewable only by reading its code.",
                ))
            case "untested":
                issues.append(WorkflowIssue(
                    kind="untested", stage_id=stage.id, stage_name=stage.name,
                    detail="Has a description, but nothing has checked it against the code.",
                ))
            case "untestable":
                # A filter_rows or publish carries a description but is not a
                # STAGE_TEST_TYPE, so no example can ever certify it. Raised rather
                # than dropped: the reviewer needs to know this description is
                # unverifiable, even though the fix is not on the stage.
                issues.append(WorkflowIssue(
                    kind="untestable", stage_id=stage.id, stage_name=stage.name,
                    detail=(
                        f"A {stage.type} cannot carry examples, so its description "
                        f"cannot be checked against its code."
                    ),
                ))
    order = list(_ISSUE_SEVERITY)
    return sorted(issues, key=lambda issue: order.index(issue.kind))


def _carries_authored_code(stage: Stage) -> bool:
    """Is this stage's behaviour authored code a reviewer would otherwise have to
    read? True for the `function` and `filter` handles — the two that ask for a
    `summary` — and false for a stage fixed entirely by config."""
    return stage.function is not None or stage.filter is not None


def _summary_of(stage: Stage) -> Optional[str]:
    """The stage's plain-language summary, off whichever authored-code handle it
    carries."""
    for handle in (stage.function, stage.filter):
        if handle is not None and handle.summary:
            return handle.summary
    return None


def shape_test_views(stage: Optional[Stage]) -> list[dict[str, Any]]:
    """Pair each of `stage`'s authored tests with its run result, shaped for
    _stage_tests.html ([] for no stage, or a stage with no tests)."""
    if stage is None or not stage.tests:
        return []
    results = run_tests_for_stage(stage)
    return [
        _shape_one_test(test, result)
        for test, result in zip(stage.tests, results)
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
        "expected": {"columns": _list_row_columns(test.expected), "rows": test.expected},
        "diffs": [
            {"row": diff.row, "column": diff.column,
             "expected": diff.expected, "actual": diff.actual}
            for diff in result.diffs
        ],
    }


def _list_row_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Column order for rendering: first-appearance order across the rows."""
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key)
    return list(seen)
