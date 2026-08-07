"""GET /project/{project}/export.zip — stream a project's whole on-disk tree as
one zip download (see docs/deploy.md). Distinct from the review packet (one run,
curated) and from services.project.export_project (workflow definition as JSON).
"""
from __future__ import annotations

import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.web.config import projects_dir

router = APIRouter()

# The archive is assembled in a spooled temp file — held in memory up to this
# many bytes, spilled to disk past it — then streamed out in chunks, so a
# project with large run outputs is never materialized as one bytes object.
_SPOOL_MAX_BYTES = 32 * 1024 * 1024
_STREAM_CHUNK_BYTES = 1024 * 1024


@router.get("/project/{project}/export.zip")
async def download_project_zip(project: str) -> StreamingResponse:
    """404 on anything but a directory directly under the projects root."""
    root = projects_dir().resolve()
    # Same guard as the project pages: no traversal, no absolute path.
    target = (root / project).resolve()
    if target.parent != root or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    # A file inside the project that cannot be read fails the whole download
    # (OSError propagates as a 500) — a partial archive that silently dropped
    # a run output would be worse than no archive.
    spool = await run_in_threadpool(_zip_directory_to_spool, target)
    return StreamingResponse(
        _read_and_close(spool),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project}-export.zip"'},
    )


def _zip_directory_to_spool(
    directory: Path,
) -> tempfile.SpooledTemporaryFile[bytes]:
    """Zip every regular file under `directory`, arcnames relative to it."""
    spool: tempfile.SpooledTemporaryFile[bytes] = tempfile.SpooledTemporaryFile(
        max_size=_SPOOL_MAX_BYTES
    )
    with zipfile.ZipFile(spool, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        # Sorted, so two exports of one unchanged tree list members identically.
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=path.relative_to(directory).as_posix())
    spool.seek(0)
    return spool


def _read_and_close(spool: tempfile.SpooledTemporaryFile[bytes]) -> Iterator[bytes]:
    """Chunked reads; the finally releases the spool on a dropped connection."""
    try:
        # Starlette iterates a sync generator in its threadpool.
        while chunk := spool.read(_STREAM_CHUNK_BYTES):
            yield chunk
    finally:
        spool.close()
