"""Configure the document store for the standalone `python -m app.seeds` CLI.

A server configures one in app.main's lifespan and tests in an autouse fixture; a
CLI process has neither. Composition-root wiring kept out of seed.py — the
import-linter contract carves out only this module in ignore_imports."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.persistence import SqliteKvStore, configure_store, is_store_configured


def ensure_store_configured() -> None:
    """Configure the process-wide document store if nothing has yet, mirroring
    app.main's lifespan: an on-disk SqliteKvStore at CARBONPAPER_DB_PATH (default
    data/app.db). A no-op when a store is already set — the test suite's autouse
    in-memory store wins, and this never overrides a configured one."""
    if is_store_configured():
        return
    db_path = os.environ.get("CARBONPAPER_DB_PATH", "data/app.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    configure_store(SqliteKvStore(db_path))
