"""Download a run as a review packet — see app/services/review_packet/packet.py."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from starlette.concurrency import run_in_threadpool

from app.core.errors import RunNotFoundError
from app.web.export import export_review_packet

router = APIRouter()


@router.get("/project/{project}/runs/{run_id}/packet.zip")
async def download_review_packet(project: str, run_id: str):
    """Built in a temp directory and streamed, so exporting leaves nothing behind
    in the project on disk."""
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
    """`make_archive` needs a real path, so the zip is read back before cleanup."""
    with tempfile.TemporaryDirectory() as tmp:
        packet = export_review_packet(project, run_id, Path(tmp) / "packet")
        archive = shutil.make_archive(
            str(Path(tmp) / "archive"),
            "zip",
            root_dir=packet.root.parent,
            base_dir=packet.root.name,
        )
        return Path(archive).read_bytes()
