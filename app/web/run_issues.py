"""The run page's issue index: the stage that stopped the run, and the advisory
issues on the stages that ran. Every entry is one line and a link — the detail
stays in the stage panel's own validation block, so there is one copy of it.
"""

from __future__ import annotations

from collections import Counter
from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel

from app.core.run_status import StageStatus
from app.models import Stage, StepRefused
from app.models.run_manifest import SCHEMA_REFUSAL_ERROR_TYPE
from app.runtime.validation import Severity
from app.web.stage_strip import read_stage_records


class StopKind(str, Enum):
    """`schema`/`refused` are the data's story; `crash` is the code's."""

    schema = "schema"    # the output carries values its declared schema forbids
    refused = "refused"  # authored code raised StepRefused on what it was given
    crash = "crash"      # anything else raised, a per-row generation failure included


# The `StageErrorInfo.type` each non-crash kind is recorded under.
_KIND_BY_ERROR_TYPE = {
    SCHEMA_REFUSAL_ERROR_TYPE: StopKind.schema,
    StepRefused.__name__: StopKind.refused,
}


class ValidationIssue(BaseModel):
    """One issue, worded by the report that raised it — never re-worded here."""

    severity: str
    column: str | None
    message: str
    # Every report of the same stage that raised this same line: "output",
    # "input:<producer id>".
    phases: list[str]


class StoppedStage(BaseModel):
    """A stage whose failure ended the run."""

    stage_id: str
    kind: StopKind
    error_type: str
    error_message: str
    traceback: str | None
    issues: list[ValidationIssue]
    # Downstream of this stage and never reached. Empty when the pinned version
    # could not be read: its edges are the only thing that says so.
    never_ran: list[str]

    @property
    def refused_data(self) -> bool:
        """Which of the two stories the page tells — they route to different people."""
        return self.kind is not StopKind.crash


class FlaggedStage(BaseModel):
    """One stage's issues that did not stop the run."""

    stage_id: str
    issues: list[ValidationIssue]


class RunIssues(BaseModel):
    stopped: list[StoppedStage]
    flagged: list[FlaggedStage]

    @property
    def flagged_headline(self) -> str:
        """The flagged section's title: its counts by severity, a zero left out entirely."""
        counts = Counter(
            issue.severity for stage in self.flagged for issue in stage.issues
        )
        return ", ".join(
            f"{counts[severity.value]} {severity.value}"
            f"{'' if counts[severity.value] == 1 else 's'}"
            for severity in (Severity.warning, Severity.error)
            if counts[severity.value]
        )


def build_run_issues(
    manifest: Mapping[str, Any], stages: Sequence[Stage] | None
) -> RunIssues:
    """`stages` are the pinned version's, or None when that version could not be read."""
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
        traceback=_read_optional_text(error.get("traceback")),
        issues=[
            issue
            for issue in _read_stage_issues(record)
            if issue.severity == Severity.error
        ],
        never_ran=_find_stages_it_blocked(stage_id, consumers, never_ran, order),
    )


def _view_flagged_stages(
    records: Sequence[Mapping[str, Any]],
) -> list[FlaggedStage]:
    """Every issue the stopped section does not carry, by stage, in the run's own order."""
    flagged = []
    for record in records:
        stopped = record.get("status") == StageStatus.ERROR
        issues = [
            issue
            for issue in _read_stage_issues(record)
            if not (stopped and issue.severity == Severity.error)
        ]
        if issues:
            flagged.append(
                FlaggedStage(stage_id=_read_stage_id(record), issues=issues)
            )
    return flagged


def _read_stage_issues(record: Mapping[str, Any]) -> list[ValidationIssue]:
    """One line per (severity, column, message) over the stage's reports, errors first."""
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
    return sorted(lines, key=lambda issue: issue.severity != Severity.error)


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
    """The stages downstream of `stage_id` that never ran, in the run's own order."""
    reached: set[str] = set()
    frontier = [stage_id]
    while frontier:
        for consumer in consumers.get(frontier.pop(), ()):
            if consumer not in reached:
                reached.add(consumer)
                frontier.append(consumer)
    return [stage for stage in order if stage in reached and stage in never_ran]


def _index_consumers(stages: Sequence[Stage] | None) -> dict[str, list[str]]:
    """Producer stage id -> the stages that read it."""
    consumers: dict[str, list[str]] = {}
    for stage in stages or ():
        for ref in stage.inputs:
            consumers.setdefault(ref.id, []).append(stage.id)
    return consumers


def _read_stage_id(record: Mapping[str, Any]) -> str:
    return str(record.get("stage_id") or "")


def _read_optional_text(value: object) -> str | None:
    """Absent stays absent, never "" — a column of none is not a column named nothing."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
