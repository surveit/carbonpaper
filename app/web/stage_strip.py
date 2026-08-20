"""The stage strip: one square per stage of a run, coloured by that stage's
status, plus the labelled counts beneath it. Rendered by `_stage_strip.html` on
both the run page and the runs index."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from pydantic import BaseModel

from app.core.run_status import RunStatus, StageStatus


class StageSquare(BaseModel):
    # `status` is rendered verbatim as the `status-<value>` CSS class.
    stage_id: str
    status: str


class StatusCount(BaseModel):
    status: str
    label: str
    count: int


class StageStrip(BaseModel):
    squares: list[StageSquare]
    counts: list[StatusCount]


def build_stage_strip(manifest: Mapping[str, Any]) -> StageStrip:
    squares = [
        StageSquare(
            stage_id=str(record.get("stage_id", "")),
            status=str(record.get("status", "")),
        )
        for record in read_stage_records(manifest)
    ]
    return StageStrip(
        squares=squares,
        counts=_build_counts(squares, manifest.get("status")),
    )


def describe_stage_status(status: str, run_status: object) -> str:
    """The word a reader gets for one square; empty for a status this app does not define."""
    try:
        stage_status = StageStatus(status)
    except ValueError:
        return ""
    return _read_count_label(stage_status, run_status)


def describe_stage_counts(strip: StageStrip) -> str:
    return " · ".join(f"{c.count} {c.label}" for c in strip.counts)


def read_stage_records(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(manifest.get("stage_records") or [])


def count_stage_status(manifest: Mapping[str, Any], status: StageStatus) -> int:
    return sum(1 for r in read_stage_records(manifest) if r.get("status") == status)


_STATUS_LABEL = {
    StageStatus.OK: "done",
    StageStatus.VALIDATION_WARNINGS: "with warnings",
    StageStatus.RUNNING: "running",
    StageStatus.AWAITING_REVIEW: "waiting on you",
    StageStatus.ERROR: "failed",
    StageStatus.CANCELLED: "cancelled",
}
# Why the remaining stages have not run depends on what stopped the run, so the
# pending label is read off the run's own status rather than fixed.
_PENDING_LABEL = {
    RunStatus.RUNNING: "still to do",
    RunStatus.AWAITING_REVIEW: "blocked behind it",
    RunStatus.ERRORS: "not reached",
    RunStatus.CANCELLED: "never ran",
}
_PENDING_LABEL_OTHERWISE = "never ran"
# Display order for the counts: what finished, then what is in flight, then what
# needs a human, then what went wrong, then what never ran. Every one of the
# seven stage statuses appears, so a count can never go missing.
_COUNT_ORDER = (
    StageStatus.OK,
    StageStatus.VALIDATION_WARNINGS,
    StageStatus.RUNNING,
    StageStatus.AWAITING_REVIEW,
    StageStatus.ERROR,
    StageStatus.CANCELLED,
    StageStatus.PENDING,
)


def _build_counts(
    squares: Sequence[StageSquare], run_status: object
) -> list[StatusCount]:
    counts = Counter(square.status for square in squares)
    return [
        StatusCount(
            status=str(status),
            count=counts[str(status)],
            label=_read_count_label(status, run_status),
        )
        for status in _COUNT_ORDER
        if counts[str(status)]
    ]


def _read_count_label(status: StageStatus, run_status: object) -> str:
    if status is not StageStatus.PENDING:
        return _STATUS_LABEL[status]
    stopped_by = next((s for s in _PENDING_LABEL if s == run_status), None)
    if stopped_by is None:
        return _PENDING_LABEL_OTHERWISE
    return _PENDING_LABEL[stopped_by]
