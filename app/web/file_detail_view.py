"""One file's page: what it is, what is claimed of it, and what is in it."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.file_shape import ColumnKind, ColumnShape
from app.core.files import FileCompleteness, ProjectFile, open_project_file
from app.core.source_files import find_file_format
from app.services.frame_profile import read_file_shape
from app.web.file_preview import FilePreview, build_file_preview
from app.web.file_sizes import describe_bytes
from app.web.files_view import count_runs_by_file
from app.runtime.manifest import list_run_entries


class FacetValue(BaseModel):
    """`absent` marks the empty string and the null, which rank with the values but are not."""

    value: str
    count: int
    share: float
    absent: bool


class ColumnRow(BaseModel):
    column: str
    kind: ColumnKind
    filled_percent: float
    blank_percent: float
    null_percent: float
    filled_count: int
    blank_count: int
    null_count: int
    distinct_count: int
    group: str
    facet: list[FacetValue]
    hidden_value_count: int
    shape: ColumnShape


class ReadingRun(BaseModel):
    run_id: str
    started_at: str
    status: str


class FileContents(BaseModel):
    row_count: int
    column_count: int
    varying_count: int
    constant_count: int
    empty_count: int
    columns: list[ColumnRow]
    preview: FilePreview


class FileDetailView(BaseModel):
    file_id: str
    filename: str
    sha256: str
    size: str
    added: str
    format: str
    completeness: FileCompleteness
    lineage: str
    # None for a file no reader here opens — a png someone attached to a conversation.
    contents: FileContents | None
    runs: list[ReadingRun]


def build_file_detail_view(project_id: str, file_id: str) -> FileDetailView:
    record, _ = open_project_file(project_id, file_id)
    return FileDetailView(
        file_id=record.id, filename=record.filename, sha256=record.sha256,
        size=describe_bytes(record.byte_count), added=record.created_at,
        format=_name_format(record), completeness=record.completeness,
        lineage=record.lineage,
        contents=_read_contents(project_id, file_id) if find_file_format(record.filename)
        else None,
        runs=_find_reading_runs(project_id, record),
    )


def _read_contents(project_id: str, file_id: str) -> FileContents:
    shape = read_file_shape(project_id, file_id)
    columns = [build_column_row(column, shape.row_count) for column in shape.columns]
    return FileContents(
        row_count=shape.row_count, column_count=len(shape.columns),
        varying_count=sum(1 for row in columns if row.group == "varying"),
        constant_count=sum(1 for row in columns if row.group == "constant"),
        empty_count=sum(1 for row in columns if row.group == "empty"),
        columns=columns, preview=build_file_preview(project_id, file_id),
    )


def build_column_row(column: ColumnShape, rows: int) -> ColumnRow:
    return ColumnRow(
        column=column.column, kind=column.kind,
        filled_percent=_share(column.filled_count, rows),
        blank_percent=_share(column.blank_count, rows),
        null_percent=_share(column.null_count, rows),
        filled_count=column.filled_count, blank_count=column.blank_count,
        null_count=column.null_count, distinct_count=column.distinct_count,
        group=_group_column(column.kind),
        facet=_rank_facet(column, rows),
        hidden_value_count=max(column.distinct_count - len(column.top), 0),
        shape=column,
    )


def _rank_facet(column: ColumnShape, rows: int) -> list[FacetValue]:
    """The empty string and the null rank among the values, by how many rows each holds."""
    entries = [FacetValue(value=seen.value, count=seen.count,
                          share=_share(seen.count, rows), absent=False)
               for seen in column.top]
    for label, count in (("(empty string)", column.blank_count), ("(null)", column.null_count)):
        if count:
            entries.append(FacetValue(value=label, count=count,
                                      share=_share(count, rows), absent=True))
    return sorted(entries, key=lambda entry: -entry.count)


def _group_column(kind: ColumnKind) -> str:
    if kind == ColumnKind.EMPTY:
        return "empty"
    return "constant" if kind == ColumnKind.CONSTANT else "varying"


def _find_reading_runs(project_id: str, record: ProjectFile) -> list[ReadingRun]:
    """Newest first, off each run's own manifest — what ran, not what a version now names."""
    read_by = set(count_runs_by_file(project_id).get(record.sha256, []))
    # raw, not the typed manifest: that is what counted the reads.
    return [ReadingRun(run_id=entry.run_id,
                       started_at=str((entry.raw or {}).get("started_at") or ""),
                       status=str((entry.raw or {}).get("status") or "unrecorded"))
            for entry in reversed(list_run_entries(project_id))
            if entry.run_id in read_by]


def _name_format(record: ProjectFile) -> str:
    return record.filename.rsplit(".", 1)[-1].upper() if "." in record.filename else "FILE"


def _share(part: int, whole: int) -> float:
    return 0.0 if not whole else 100 * part / whole
