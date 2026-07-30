"""Shaping for the examples report and the certification badge, shared by every
host that renders a stage panel. Lives here rather than in app/services because
running an example needs app.runtime, which those services may not import.
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
    and on how many examples.

    The claim `certified` licenses is narrow, and the template says so out loud:
    the examples are authored from the methodology, never by running the code, so
    passing them means the summary and the code agree ON THOSE EXAMPLES. It is not
    a proof of correctness and says nothing about inputs no example covers."""

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


def _carries_authored_code(stage: Stage) -> bool:
    """Is this stage's behaviour authored code a reviewer would otherwise have to
    read? True for the `function` and `filter` blocks — the two that ask for a
    `summary` — and false for a stage fixed entirely by config."""
    return stage.find_authored_code_block() is not None


def _summary_of(stage: Stage) -> Optional[str]:
    """The stage's plain-language summary, off whichever authored-code block it
    carries."""
    block = stage.find_authored_code_block()
    return block.summary if block is not None else None


def shape_test_views(stage: Optional[Stage]) -> list[dict[str, Any]]:
    """Pair each of `stage`'s authored examples with its run result, shaped for
    _stage_tests.html ([] for no stage, or a stage carrying none)."""
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
        # None, not an empty table: a failure case claims the step must fail, which
        # the template must not render as "succeeded, returned nothing".
        "expected": None if test.expected is None else {
            "columns": _list_row_columns(test.expected), "rows": test.expected
        },
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
