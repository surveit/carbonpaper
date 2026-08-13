"""What the Files page shows: every file a project holds, and which of its runs read
each one — the only column that says whether a file can go."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel

from app.models.run_manifest import find_manifest_backed_run_dirs
from app.services import uploads
from app.services.workspace import resolve_project_dir
from app.web.file_sizes import describe_bytes


class FileRow(BaseModel):
    sha256: str
    filename: str
    size: str
    added: str
    run_count: int
    last_run_id: str | None


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
    records = uploads.list_project_files(project_id)
    used, quota = uploads.measure_files_used_bytes(), uploads.files_quota_bytes()
    return FilesView(
        rows=[_build_row(record, reads.get(record.sha256, [])) for record in records],
        # Summed off the records, not the disk: the disk is shared, and one project's
        # files are the ones it holds.
        project_used=describe_bytes(sum(record.byte_count for record in records)),
        used=describe_bytes(used),
        quota=describe_bytes(quota),
        used_percent=min(used / quota * 100, 100) if quota else 100,
        max_upload=describe_bytes(uploads.max_upload_bytes()),
    )


def count_runs_by_file(project_id: str) -> dict[str, list[str]]:
    """sha256 -> the run ids that read it, oldest first."""
    runs = defaultdict(list)
    # Off each run's own manifest, because that is where what a run ACTUALLY read is
    # recorded — the version it pinned may since have been edited to name another file.
    for run_dir in find_manifest_backed_run_dirs(resolve_project_dir(project_id) / "runs"):
        for sha256 in _read_input_hashes(run_dir):
            runs[sha256].append(run_dir.name)
    return runs


def _read_input_hashes(run_dir: Path) -> list[str]:
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A torn or unreadable manifest costs this run's rows a mention, not the page.
        return []
    bindings = manifest.get("input_bindings") or {}
    return [binding["sha256"] for binding in bindings.values()
            if isinstance(binding, dict) and binding.get("sha256")]


def _build_row(record: uploads.UploadedFile, run_ids: list[str]) -> FileRow:
    return FileRow(
        sha256=record.sha256,
        filename=record.filename,
        size=describe_bytes(record.byte_count),
        added=record.created_at,
        run_count=len(run_ids),
        last_run_id=run_ids[-1] if run_ids else None,
    )
