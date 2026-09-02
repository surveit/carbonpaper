"""The issue index: the stage that stopped the run, and the advisory issues on the
stages that ran. One line each, worded by the report that raised it and linking the
stage it came from. Rendered by the run page and by the review packet's index; the
stage panel repeats none of it, so each surface holds one copy.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel

from app.core.run_status import StageStatus
from app.models import StepRefused, WorkflowStage
from app.models.run_manifest import SCHEMA_REFUSAL_ERROR_TYPE
from app.models.severity import UserFacingErrorSeverity
from app.web.stage_strip import read_stage_records


class StopKind(str, Enum):
    schema = "schema"    # the output carries values its declared schema forbids
    refused = "refused"  # authored code raised StepRefused on what it was given
    crash = "crash"      # anything else raised, a per-row generation failure included


# The `StageErrorInfo.type` each non-crash kind is recorded under.
_KIND_BY_ERROR_TYPE = {
    SCHEMA_REFUSAL_ERROR_TYPE: StopKind.schema,
    StepRefused.__name__: StopKind.refused,
}


class ValidationIssue(BaseModel):
    severity: str
    column: str | None
    message: str
    # Every report of the same stage that raised this same line: "output",
    # "input:<producer id>".
    phases: list[str]


class StoppedStage(BaseModel):
    stage_id: str
    kind: StopKind
    error_type: str
    error_message: str
    issues: list[ValidationIssue]
    # Downstream of this stage and never reached. Empty when the pinned version
    # could not be read: its edges are the only thing that says so.
    never_ran: list[str]


class FlaggedStage(BaseModel):
    stage_id: str
    issues: list[ValidationIssue]


class RunIssues(BaseModel):
    stopped: list[StoppedStage]
    flagged: list[FlaggedStage]

    # The two counts the panel is headed by; the WORDING is the shared issue
    # table's, so the Workflow page's heading cannot drift from this one.
    @property
    def error_count(self) -> int:
        return len(self.stopped) + self._count_flagged(UserFacingErrorSeverity.error)

    @property
    def warning_count(self) -> int:
        return self._count_flagged(UserFacingErrorSeverity.warning)

    def _count_flagged(self, severity: UserFacingErrorSeverity) -> int:
        return sum(
            1
            for stage in self.flagged
            for issue in stage.issues
            if issue.severity == severity
        )


def build_run_issues(
    manifest: Mapping[str, Any], stages: Sequence[WorkflowStage] | None
) -> RunIssues:
    records = read_stage_records(manifest)
    # Only their edges say which never-ran stage a given stop is what blocked, so
    # without them a stop names none rather than blaming the ones it can see.
    order = [_read_stage_id(record) for record in records]
    consumers = _index_consumers(stages)
    never_ran = {
        _read_stage_id(record)
        for record in records
        if record.get("status") == StageStatus.PENDING
    }
    return RunIssues(
        stopped=[
            _view_stopped_stage(record, consumers, never_ran, order)
            for record in records
            if record.get("status") == StageStatus.ERROR
        ],
        flagged=_view_flagged_stages(records),
    )


def _view_stopped_stage(
    record: Mapping[str, Any],
    consumers: Mapping[str, list[str]],
    never_ran: set[str],
    order: Sequence[str],
) -> StoppedStage:
    error = record.get("error") or {}
    stage_id = _read_stage_id(record)
    error_type = str(error.get("type") or "")
    return StoppedStage(
        stage_id=stage_id,
        kind=_KIND_BY_ERROR_TYPE.get(error_type, StopKind.crash),
        error_type=error_type,
        error_message=str(error.get("message") or ""),
        issues=[
            issue
            for issue in _read_stage_issues(record)
            if issue.severity == UserFacingErrorSeverity.error
        ],
        never_ran=_find_stages_it_blocked(stage_id, consumers, never_ran, order),
    )


def _view_flagged_stages(
    records: Sequence[Mapping[str, Any]],
) -> list[FlaggedStage]:
    flagged = []
    for record in records:
        stopped = record.get("status") == StageStatus.ERROR
        issues = [
            issue
            for issue in _read_stage_issues(record)
            if not (stopped and issue.severity == UserFacingErrorSeverity.error)
        ]
        if issues:
            flagged.append(
                FlaggedStage(stage_id=_read_stage_id(record), issues=issues)
            )
    return flagged


def _read_stage_issues(record: Mapping[str, Any]) -> list[ValidationIssue]:
    phases_by_issue: dict[tuple[str, str | None, str], list[str]] = {}
    # A passed-through column raises the identical line on the input side and the
    # output side; that is one thing to look at, not two.
    for report in _read_reports(record):
        phase = str(report.get("phase") or "")
        for issue in report.get("issues") or []:
            key = (
                str(issue.get("severity") or ""),
                _read_optional_text(issue.get("column")),
                str(issue.get("message") or ""),
            )
            phases = phases_by_issue.setdefault(key, [])
            if phase not in phases:
                phases.append(phase)
    lines = [
        ValidationIssue(severity=severity, column=column, message=message, phases=phases)
        for (severity, column, message), phases in phases_by_issue.items()
    ]
    return sorted(lines, key=lambda issue: issue.severity != UserFacingErrorSeverity.error)


def _read_reports(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    reports = [
        report
        for report in record.get("input_validation_report") or []
        if isinstance(report, Mapping)
    ]
    output = record.get("output_validation_report")
    if isinstance(output, Mapping):
        reports.append(output)
    return reports


def _find_stages_it_blocked(
    stage_id: str,
    consumers: Mapping[str, list[str]],
    never_ran: set[str],
    order: Sequence[str],
) -> list[str]:
    reached: set[str] = set()
    frontier = [stage_id]
    while frontier:
        for consumer in consumers.get(frontier.pop(), ()):
            if consumer not in reached:
                reached.add(consumer)
                frontier.append(consumer)
    return [stage for stage in order if stage in reached and stage in never_ran]


def _index_consumers(stages: Sequence[WorkflowStage] | None) -> dict[str, list[str]]:
    consumers: dict[str, list[str]] = {}
    for stage in stages or ():
        for source in stage.inputs:
            consumers.setdefault(source.id, []).append(stage.id)
    return consumers


def _read_stage_id(record: Mapping[str, Any]) -> str:
    return str(record.get("stage_id") or "")


def _read_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
