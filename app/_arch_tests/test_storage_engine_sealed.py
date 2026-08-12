"""Architecture: the storage engine is spoken only by the document store.

No subsystem opens a database itself: records persist through ``PersistedModel``,
frames through ``FrameStore``. Scope is all of ``app/``; ``examples/`` and scratch
dirs are out of scope by design.
"""
from __future__ import annotations

from arch import check_no_import, find_governed_files


def test_sqlite3_imported_only_by_the_store() -> None:
    offenders = check_no_import(
        find_governed_files(__file__),
        "sqlite3",
        allow={"app/core/sqlite_store.py"},
    )
    assert not offenders, (
        "sqlite3 must be imported only by app/core/sqlite_store.py (the engine); "
        "every other module persists via PersistedModel / FrameStore:\n  "
        + "\n  ".join(offenders)
    )
