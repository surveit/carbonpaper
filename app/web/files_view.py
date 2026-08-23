"""What the Files page shows: every file a project holds, and which of its runs read
each one — the only column that says whether a file can go."""
from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel

from app.core.errors import FileNotStoredError
from app.core.file_comparison import (
    ShapeGroup, choose_the_telling_column, group_files_by_columns, read_leading_value,
)
from app.core.file_shape import FileShape
from app.core.json_types import JsonDict
from app.models.run_manifest import read_input_bindings
from app.runtime.manifest import list_run_entries
from app.core import files as file_store
from app.services.frame_profile import read_file_shape
from app.web.file_sizes import describe_bytes


class Distinction(BaseModel):
    """What separates this file from the others of its shape, in their own values."""

    column: str
    value: str
    share: float


class FileRow(BaseModel):
    # The handle a link carries; the hash is shown beside it as evidence about the bytes.
    file_id: str
    sha256: str
    filename: str
    size: str
    added: str
    run_count: int
    completeness: file_store.FileCompleteness
    lineage: str
    # None when nothing here shares its columns, or the sharers carry the same values.
    distinction: Distinction | None = None
    # How many files carry these columns, this one included.
    shape_group_size: int = 1
    # Which files group together, and what to call the group.
    shape_key: str = ""
    shape_label: str = ""


class FilesView(BaseModel):
    rows: list[FileRow]
    # This project's own files, which is what the page is about, and the store they share
    # with every other project, which is what the quota bounds.
    project_used: str
    used: str
    quota: str
    # 0–100, for the meter's width. A store past its quota still draws a full bar
    # rather than one overflowing its track.
    used_percent: float
    max_upload: str


def build_files_view(project_id: str) -> FilesView:
    reads = count_runs_by_file(project_id)
    records = file_store.list_project_files(project_id)
    used, quota = file_store.measure_files_used_bytes(), file_store.files_quota_bytes()
    shapes = _read_shapes(project_id, records)
    distinctions = find_what_tells_them_apart(shapes)
    groups = group_files_by_columns(shapes)
    return FilesView(
        rows=[_build_row(record, reads.get(record.sha256, []),
                         distinctions.get(record.id), _find_group(groups, record.id))
              for record in records],
        # Summed off the records, not the disk: the disk is shared, and one project's
        # files are the ones it holds.
        project_used=describe_bytes(sum(record.byte_count for record in records)),
        used=describe_bytes(used),
        quota=describe_bytes(quota),
        used_percent=min(used / quota * 100, 100) if quota else 100,
        max_upload=describe_bytes(file_store.max_upload_bytes()),
    )


def count_runs_by_file(project_id: str) -> dict[str, list[str]]:
    """sha256 -> the run ids that read it, oldest first."""
    runs = defaultdict(list)
    # Off each run's own manifest, because that is where what a run ACTUALLY read is
    # recorded — the version it pinned may since have been edited to name another file.
    # A manifest names the BYTES it read, not the record it read them from, so two
    # records holding identical bytes each count every run over either of them.
    for entry in list_run_entries(project_id):
        for sha256 in _read_input_hashes(entry.raw):
            runs[sha256].append(entry.run_id)
    return runs


def _read_input_hashes(manifest: JsonDict | None) -> list[str]:
    if manifest is None:
        # An unreadable manifest costs this run's rows a mention, not the page.
        return []
    return [b.sha256 for b in read_input_bindings(manifest) if b.sha256]


def find_what_tells_them_apart(
    shapes: dict[str, FileShape],
) -> dict[str, Distinction]:
    """Per file: the most disagreed-about column among the files sharing its columns."""
    found: dict[str, Distinction] = {}
    for group in group_files_by_columns(shapes):
        if len(group.file_ids) < 2:
            continue
        telling = choose_the_telling_column([shapes[file_id] for file_id in group.file_ids])
        if telling is None:
            continue
        for file_id in group.file_ids:
            told = _describe_leading_value(shapes[file_id], telling.column)
            if told is not None:
                found[file_id] = told
    return found


def _describe_leading_value(shape: FileShape, column: str) -> Distinction | None:
    leading = read_leading_value(shape, column)
    if leading is None:
        return None
    filled = next(c.filled_count for c in shape.columns if c.column == column)
    return Distinction(column=column, value=leading.value,
                       share=100 * leading.count / filled)


def _read_shapes(
    project_id: str, records: list[file_store.ProjectFile]
) -> dict[str, FileShape]:
    """A file no reader here opens has no shape and takes no part in the comparison."""
    shapes = {}
    for record in records:
        try:
            shapes[record.id] = read_file_shape(project_id, record.id)
        except (ValueError, FileNotStoredError):
            continue
    return shapes


def _find_group(groups: list[ShapeGroup], file_id: str) -> ShapeGroup | None:
    return next((group for group in groups if file_id in group.file_ids), None)


def _name_group(group: ShapeGroup | None) -> str:
    if group is None:
        return "not a table"
    files = len(group.file_ids)
    return (f"{len(group.columns)} columns · {files} file"
            f"{'' if files == 1 else 's'}")


def _build_row(record: file_store.ProjectFile, run_ids: list[str],
               distinction: Distinction | None, group: ShapeGroup | None) -> FileRow:
    return FileRow(
        file_id=record.id,
        sha256=record.sha256,
        filename=record.filename,
        size=describe_bytes(record.byte_count),
        added=record.created_at,
        run_count=len(run_ids),
        completeness=record.completeness,
        lineage=record.lineage,
        distinction=distinction,
        shape_group_size=len(group.file_ids) if group else 1,
        shape_key="|".join(group.columns) if group else "",
        shape_label=_name_group(group),
    )
