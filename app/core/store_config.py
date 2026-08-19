"""Default process-wide storage wiring, one place so the server and the CLI
cannot drift onto different databases: a run started from the command line must
land in the same store the web UI reads back. Also hosts `refuse_renamed_env_vars`,
the guard over the whole CARBON_PAPER_* set that every composition root calls first.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.core.frames import FrameStore, configure_frame_store, is_frame_store_configured
from app.core.paths import CARBON_PAPER_HOME
from app.core.persistence import configure_store, is_store_configured
from app.core.sqlite_store import SqliteKvStore
from app.core.transform_model_settings import initialize_transform_model_setting

_RENAMED_ENV_PREFIX = "CARBONPAPER_"
_ENV_PREFIX = "CARBON_PAPER_"


def refuse_renamed_env_vars() -> None:
    # Reading past one would boot on default paths against an empty store: data loss, silently.
    stale = sorted(name for name in os.environ if name.startswith(_RENAMED_ENV_PREFIX))
    if not stale:
        return
    raise RuntimeError(
        f"{_RENAMED_ENV_PREFIX}* environment variables were renamed and are no longer "
        f"read; unset each one and set its replacement: "
        + ", ".join(f"{name} -> {_renamed(name)}" for name in stale)
    )


def configure_default_stores() -> None:
    configure_default_document_store()
    initialize_transform_model_setting()
    _configure_default_frame_store()


def configure_default_document_store() -> None:
    if is_store_configured():
        return
    configure_store(SqliteKvStore(str(create_db_directory())))


def create_db_directory() -> Path:
    """sqlite opens no database under a missing directory; alembic needs it made too."""
    db_path = resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def resolve_db_path() -> Path:
    override = os.environ.get("CARBON_PAPER_DB_PATH")
    return Path(override) if override is not None else CARBON_PAPER_HOME / "app.db"


def _renamed(name: str) -> str:
    return _ENV_PREFIX + name[len(_RENAMED_ENV_PREFIX):]


def _configure_default_frame_store() -> None:
    """The default root sits beside the DB, so pinning CARBON_PAPER_DB_PATH carries the frames too."""
    if is_frame_store_configured():
        return
    override = os.environ.get("CARBON_PAPER_FRAMES_ROOT")
    root = Path(override) if override is not None else resolve_db_path().parent / "frames"
    configure_frame_store(FrameStore(root))
