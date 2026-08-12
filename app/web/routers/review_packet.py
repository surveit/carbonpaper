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
_COMPRESS_LEVEL = 1


@router.get("/project/{project}/runs/{run_id}/packet.zip")
async def download_review_packet(project: str, run_id: str):
    try:
        content = await run_in_threadpool(_build_packet_zip, project, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    filename = f"{project}__{run_id}__review-packet.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
