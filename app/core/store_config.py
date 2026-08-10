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
    _configure_default_document_store()
    _configure_default_frame_store()


def _configure_default_document_store() -> None:
    if is_store_configured():
        return
    db_path = resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    configure_store(SqliteKvStore(str(db_path)))


def _configure_default_frame_store() -> None:
    """The default root sits beside the DB, so pinning CARBONPAPER_DB_PATH carries the frames too."""
    if is_frame_store_configured():
        return
    override = os.environ.get("CARBONPAPER_FRAMES_ROOT")
    root = Path(override) if override is not None else resolve_db_path().parent / "frames"
    configure_frame_store(FrameStore(root))


def resolve_db_path() -> Path:
    return Path(os.environ.get("CARBONPAPER_DB_PATH", "data/app.db"))
