"""Download a run as a review packet — see app/services/review_packet/packet.py."""
from __future__ import annotations

import logging
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from starlette.concurrency import run_in_threadpool

from app.core.errors import RunNotFoundError
from app.core.logging_config import log_elapsed
from app.web.review_packet import export_review_packet

router = APIRouter()
_log = logging.getLogger(__name__)

READY_COOKIE = "packet_ready"
_COMPRESS_LEVEL = 1


@router.get("/project/{project}/runs/{run_id}/packet.zip")
async def download_review_packet(project: str, run_id: str, ready: str | None = None):
    try:
        content = await run_in_threadpool(_build_packet_zip, project, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    filename = f"{project}__{run_id}__review-packet.zip"
    response = Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
    if ready is not None:
        # The page polls for this to stop its spinner: a download gives the browser
        # no event to fire, and the wait being timed is the packet BUILD, not the
        # transfer. Read and discarded client-side (static/packet_export.js).
        response.set_cookie(READY_COOKIE, ready, path="/", samesite="lax")
    return response


def _build_packet_zip(project: str, run_id: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        packet = export_review_packet(project, run_id, Path(tmp) / "packet")
        archive = Path(tmp) / "archive.zip"
        with log_elapsed(_log, f"{project}/{run_id} zip"):
            _write_zip(archive, packet.root)
            content = archive.read_bytes()
    _log.info("%s/%s packet is %.1f MB", project, run_id, len(content) / 1024 / 1024)
    return content


def _write_zip(archive: Path, root: Path) -> None:
    # Level 1 beat the default 6 by 2.5s for 8% more bytes, inside the request.
    with zipfile.ZipFile(
        archive, "w", zipfile.ZIP_DEFLATED, compresslevel=_COMPRESS_LEVEL
    ) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, Path(root.name) / path.relative_to(root))
