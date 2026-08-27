"""A run's review packet, as a download and as pages — app/services/review_packet/packet.py."""
from __future__ import annotations

import logging
import mimetypes
import tempfile
import threading
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.core.errors import RunNotFoundError
from app.core.logging_config import log_elapsed
from app.web.review_packet import export_review_packet

router = APIRouter()
_log = logging.getLogger(__name__)

READY_COOKIE = "packet_ready"
_COMPRESS_LEVEL = 1

PACKET_ROUTE = "packet"

# What mimetypes does not know but a reader still wants rendered rather than saved.
_TEXT_SUFFIXES = frozenset({".mmd", ".jsonl", ".md"})

# The served pages ARE the downloaded folder — one build, read back file by file, so
# the URL and the zip can never say different things. Keyed per run, and thrown away
# with the process: nothing edits a packet once it is written.
_BUILT: dict[tuple[str, str], Path] = {}
_BUILD_LOCK = threading.Lock()


@router.get("/project/{project_id}/runs/{run_id}/packet.zip")
async def download_review_packet(project_id: str, run_id: str, ready: str | None = None):
    try:
        content = await run_in_threadpool(_build_packet_zip, project_id, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    filename = f"{project_id}__{run_id}__review-packet.zip"
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


def _build_packet_zip(project_id: str, run_id: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        packet = export_review_packet(project_id, run_id, Path(tmp) / "packet")
        archive = Path(tmp) / "archive.zip"
        with log_elapsed(_log, f"{project_id}/{run_id} zip"):
            _write_zip(archive, packet.root)
            content = archive.read_bytes()
    _log.info("%s/%s packet is %.1f MB", project_id, run_id, len(content) / 1024 / 1024)
    return content


def _write_zip(archive: Path, root: Path) -> None:
    # Level 1 beat the default 6 by 2.5s for 8% more bytes, inside the request.
    with zipfile.ZipFile(
        archive, "w", zipfile.ZIP_DEFLATED, compresslevel=_COMPRESS_LEVEL
    ) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, Path(root.name) / path.relative_to(root))


@router.get("/project/{project_id}/runs/{run_id}/" + PACKET_ROUTE)
async def open_review_packet(project_id: str, run_id: str):
    return RedirectResponse(
        f"/project/{project_id}/runs/{run_id}/{PACKET_ROUTE}/index.html", status_code=307
    )


@router.get("/project/{project_id}/runs/{run_id}/" + PACKET_ROUTE + "/{path:path}")
async def read_review_packet_file(project_id: str, run_id: str, path: str):
    """The exported folder, served. Same bytes a reader would have downloaded."""
    try:
        root = await run_in_threadpool(_build_packet_folder, project_id, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail=f"{path} is not in this packet")
    return FileResponse(target, media_type=_read_media_type(target))


def _read_media_type(target: Path) -> str:
    guessed, _ = mimetypes.guess_type(target.name)
    if guessed:
        return guessed
    return (
        "text/plain; charset=utf-8"
        if target.suffix in _TEXT_SUFFIXES
        else "application/octet-stream"
    )


def _build_packet_folder(project_id: str, run_id: str) -> Path:
    with _BUILD_LOCK:
        built = _BUILT.get((project_id, run_id))
        if built is not None and built.is_dir():
            return built
        # mkdtemp, not TemporaryDirectory: the folder outlives this call by design.
        root = export_review_packet(
            project_id, run_id, Path(tempfile.mkdtemp(prefix="carbonpaper-packet-"))
        ).root.resolve()
        _BUILT[(project_id, run_id)] = root
        return root
