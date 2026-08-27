"""Typed views over a run's manifest dict, parsed once here so the data and page
writers never read raw JSON. See docs/architecture.md for the packet's shape."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.models.run_manifest import InputBinding, read_input_bindings

# A stage that never executed has no output file; the packet says so rather than
# writing an empty CSV that would read as "this stage produced nothing".
MISSING_OUTPUT = "no output file recorded"


class IssueView(BaseModel):
    severity: str
    column: str | None
    message: str


class ValidationView(BaseModel):
    label: str
    ok: bool
    rows: int | None
    issues: list[IssueView]


class StageView(BaseModel):
    record: dict[str, Any]
    stage_id: str
    type: str
    status: str
    row_count: int
    elapsed_ms: int
    error: str | None
    notes: list[str]
    output_path: str | None
    validations: list[ValidationView]
    data_file: str | None
    definition_error: str | None


class RunView(BaseModel):
    project: str
    run_id: str
    status: str
    started_at: str
    finished_at: str | None
    workflow_version: str | None
    is_test_run: bool
    bust_cache: bool
    halted_at: list[str]
    dropped_columns: dict[str, list[str]]
    stages: list[StageView]
    inputs: list[InputBinding]


def build_run_view(
    manifest: dict[str, Any],
    definition_error: str | None,
) -> RunView:
    return RunView(
        project=str(manifest.get("project") or ""),
        run_id=str(manifest.get("run_id") or ""),
        status=_read_plain_str(manifest.get("status")),
        started_at=str(manifest.get("started_at") or ""),
        finished_at=_read_optional_str(manifest, "finished_at"),
        workflow_version=_read_optional_str(manifest, "workflow_version"),
        is_test_run=bool(manifest.get("is_test_run", False)),
        bust_cache=bool(manifest.get("bust_cache", False)),
        halted_at=[str(s) for s in manifest.get("halted_at") or []],
        dropped_columns=_read_dropped_columns(manifest),
        stages=_build_stage_views(manifest, definition_error),
        inputs=read_input_bindings(manifest),
    )


def _build_stage_views(
    manifest: dict[str, Any],
    definition_error: str | None,
) -> list[StageView]:
    return [
        _build_stage_view(record, definition_error)
        for record in manifest.get("stage_records") or []
    ]


def _build_stage_view(
    record: dict[str, Any],
    definition_error: str | None,
) -> StageView:
    stage_id = str(record.get("stage_id") or "")
    output_path = _read_optional_str(record, "output_path")
    return StageView(
        record=record,
        stage_id=stage_id,
        type=_read_plain_str(record.get("type")),
        status=_read_plain_str(record.get("status")),
        row_count=int(record.get("output_row_count") or 0),
        elapsed_ms=int(record.get("elapsed_ms") or 0),
        error=_read_stage_error(record),
        notes=[str(n) for n in record.get("notes") or []],
        output_path=output_path,
        validations=_build_validation_views(record),
        data_file=f"data/{stage_id}.csv" if output_path else None,
        definition_error=definition_error,
    )


def _build_validation_views(record: dict[str, Any]) -> list[ValidationView]:
    views = [
        _build_validation_view(report, "input")
        for report in record.get("input_validation_report") or []
    ]
    output = record.get("output_validation_report")
    if isinstance(output, dict):
        views.append(_build_validation_view(output, "output"))
    return views


def _build_validation_view(report: dict[str, Any], phase: str) -> ValidationView:
    label = str(report.get("stage_id") or phase)
    rows = report.get("rows")
    return ValidationView(
        label=f"{phase} · {label}",
        ok=bool(report.get("ok", False)),
        rows=int(rows) if isinstance(rows, int) else None,
        issues=[_build_issue_view(i) for i in report.get("issues") or []],
    )


def _build_issue_view(issue: dict[str, Any]) -> IssueView:
    return IssueView(
        severity=str(issue.get("severity") or ""),
        column=_read_optional_str(issue, "column"),
        message=str(issue.get("message") or ""),
    )


def _read_stage_error(record: dict[str, Any]) -> str | None:
    error = record.get("error")
    if not isinstance(error, dict):
        return None
    return str(error.get("message") or error.get("type") or "") or None


def _read_dropped_columns(manifest: dict[str, Any]) -> dict[str, list[str]]:
    dropped = manifest.get("dropped_columns") or {}
    return {
        str(stage_id): [str(c) for c in columns]
        for stage_id, columns in dropped.items()
        if columns
    }


def _read_plain_str(value: Any) -> str:
    """The Enum's value: `str()` on a str-Enum yields a repr like "StageType.input_data"."""
    if isinstance(value, Enum):
        return str(value.value)
    return "" if value is None else str(value)


def _read_optional_str(source: dict[str, Any], key: str) -> str | None:
    value = source.get(key)
    return None if value is None else _read_plain_str(value)


LINEAGE_DIR = "lineage"


class StageTraces(BaseModel):
    stage_id: str
    rows: list[int]
    # Rows the report linked directly; the rest were reached through a trace.
    published: int
    hrefs: list[str]

    def is_report_input(self) -> bool:
        return self.published > 0


class LineageReport(BaseModel):
    written: list[str]
    traced: set[tuple[str, int]]
    refused: str | None
    stages: list[StageTraces] = []
    figures: list[PublishedFigure] = []


class PublishedFigure(BaseModel):
    """A figure the artifact printed, as the report stage named it."""

    label: str
    value: str | None
    stage_id: str
    row_ordinal: int
    href: str | None
