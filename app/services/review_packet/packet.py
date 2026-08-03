"""What an export produced. The assembling lives in app.web.export.packet — the
pages are the app's own run templates, which only the web layer may reach."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.services.review_packet.data import OmittedFile


class ReviewPacket(BaseModel):
    """`omitted` is what the packet could not include; the index renders it."""

    project: str
    run_id: str
    root: Path
    files: list[str]
    omitted: list[OmittedFile]


__all__ = ["ReviewPacket"]
