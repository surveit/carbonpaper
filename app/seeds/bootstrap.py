"""bootstrap.py — configure the document store for the STANDALONE
`python -m app.seeds` CLI entrypoint.

The seeding LOGIC (app.seeds.seed) is store-free: it composes the
app.services.project export/import seam, which assumes a store is already configured.
A long-running server configures one in app.main's lifespan, and the test
suite in an autouse fixture — but a standalone CLI process has neither, so its
entrypoint must configure the store itself before it reaches the seam (import
now snapshots a version, which lives in the document store). That is
composition-root wiring, kept OUT of the seed logic: only this entrypoint
imports the store, seed.py never does — the import-linter contract enforces it,
carving out this one module in ignore_imports."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.persistence import SqliteKvStore, configure_store, is_store_configured


def ensure_store_configured() -> None:
    """Configure the process-wide document store if nothing has yet, mirroring
    app.main's lifespan: an on-disk SqliteKvStore at CW_DB_PATH (default
    data/app.db). A no-op when a store is already set — the test suite's autouse
    in-memory store wins, and this never overrides a configured one."""
    if is_store_configured():
        return
    db_path = os.environ.get("CW_DB_PATH", "data/app.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    configure_store(SqliteKvStore(db_path))
