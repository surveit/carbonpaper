"""What an export produced. The assembling lives in app.web.review_packet.packet — the
pages are the app's own run templates, which only the web layer may reach."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.services.review_packet.data import OmittedFile


class ReviewPacket(BaseModel):
    project: str
    run_id: str
    root: Path
    files: list[str]
    omitted: list[OmittedFile]
