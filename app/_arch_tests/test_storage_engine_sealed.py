"""Architecture: the storage engine is spoken only by the document store.

``app/core/persistence.py`` owns the SQLite backend; every record persists through
``PersistedModel`` (and every frame through ``FrameStore``), so no subsystem opens a
database itself. One DB owner is what keeps the backend swappable — Postgres or plain
files later, behind ``DocumentStore`` — and keeps the rest of the app testable without
a database. Scope is all of ``app/`` (this test sits at its root); ``examples/`` and
scratch dirs are out of scope by design.
"""
from __future__ import annotations

from arch import check_no_import, find_governed_files


def test_sqlite3_imported_only_by_the_store() -> None:
    offenders = check_no_import(
        find_governed_files(__file__),
        "sqlite3",
        allow={"app/core/persistence.py"},
    )
    assert not offenders, (
        "sqlite3 must be imported only by app/core/persistence.py (the store); "
        "every other module persists via PersistedModel / FrameStore:\n  "
        + "\n  ".join(offenders)
    )
