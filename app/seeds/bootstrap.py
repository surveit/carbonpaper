"""Store + projects-root wiring for the standalone `python -m app.seeds` CLI,
which has neither app.main's lifespan nor the test fixtures. The projects root
comes from app.services.workspace, re-exported here so the CLI has one import
for its whole composition root."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.persistence import SqliteKvStore, configure_store, is_store_configured
from app.services.workspace import configure_projects_dir_from_env as configure_projects_dir_from_env


def ensure_store_configured() -> None:
    if is_store_configured():
        return
    db_path = os.environ.get("CARBONPAPER_DB_PATH", "data/app.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    configure_store(SqliteKvStore(db_path))

