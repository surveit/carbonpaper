"""What the Files page shows: every file a project holds, and which of its runs read
each one — the only column that says whether a file can go."""
from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel

from app.core.persistence import JsonDict
from app.models.run_manifest import read_input_bindings
from app.runtime.manifest import list_run_entries
from app.core import files as file_store
from app.web.file_sizes import describe_bytes


class FileRow(BaseModel):
    # The handle a link carries; the hash is shown beside it as evidence about the bytes.
    file_id: str
    sha256: str
    filename: str
    size: str
    added: str
    run_count: int


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
    return FilesView(
        rows=[_build_row(record, reads.get(record.sha256, [])) for record in records],
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


def _build_row(record: file_store.UploadedFile, run_ids: list[str]) -> FileRow:
    return FileRow(
        file_id=record.id,
        sha256=record.sha256,
        filename=record.filename,
        size=describe_bytes(record.byte_count),
        added=record.created_at,
        run_count=len(run_ids),
    )
