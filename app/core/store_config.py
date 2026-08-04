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
    """Configure the document store (`CARBONPAPER_DB_PATH`, default `data/app.db`)
    and the frame store (`CARBONPAPER_FRAMES_ROOT`, default the DB path's own
    directory + `/frames`) — each only if nothing has configured it yet."""
    _configure_default_document_store()
    _configure_default_frame_store()


def _configure_default_document_store() -> None:
    if is_store_configured():
        return
    db_path = resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    configure_store(SqliteKvStore(str(db_path)))


def _configure_default_frame_store() -> None:
    """A cache entry spans both stores — the row payload in the document store,
    the frame payload in the frame store — so the two roots must move together.
    The default frames root is computed from the document store's own location
    rather than from an independent relative literal: pinning `CARBONPAPER_DB_PATH`
    alone carries the frames with it, instead of silently leaving them resolving
    against the process's working directory, where a run launched from
    elsewhere misses every frame entry and re-pins duplicates. `CARBONPAPER_FRAMES_ROOT`
    still separates them for a caller that means to."""
    if is_frame_store_configured():
        return
    override = os.environ.get("CARBONPAPER_FRAMES_ROOT")
    root = Path(override) if override is not None else resolve_db_path().parent / "frames"
    configure_frame_store(FrameStore(root))


def resolve_db_path() -> Path:
    """Alembic's env.py reads this too, so a migration and the app cannot diverge."""
    return Path(os.environ.get("CARBONPAPER_DB_PATH", "data/app.db"))
