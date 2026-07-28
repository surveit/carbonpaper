"""Default process-wide storage wiring, one place so the server and the CLI
cannot drift onto different databases: a run started from the command line must
land in the same store the web UI reads back.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.core.frames import FrameStore, configure_frame_store, is_frame_store_configured
from app.core.persistence import SqliteKvStore, configure_store, is_store_configured


def configure_default_stores() -> None:
    """Configure the document store (`CW_DB_PATH`, default `data/app.db`) and
    the frame store (`CW_FRAMES_ROOT`, default `data/frames`) — each only if
    nothing has configured it yet."""
    _configure_default_document_store()
    _configure_default_frame_store()


def _configure_default_document_store() -> None:
    if is_store_configured():
        return
    db_path = os.environ.get("CW_DB_PATH", "data/app.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    configure_store(SqliteKvStore(db_path))


def _configure_default_frame_store() -> None:
    if is_frame_store_configured():
        return
    configure_frame_store(FrameStore(Path(os.environ.get("CW_FRAMES_ROOT", "data/frames"))))
