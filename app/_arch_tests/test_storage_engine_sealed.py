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


def test_sqlalchemy_imported_only_by_the_store_and_the_tables() -> None:
    offenders = check_no_import(
        find_governed_files(__file__),
        "sqlalchemy",
        allow={"app/core/sqlite_store.py", "app/core/table_spec.py"},
    )
    assert not offenders, (
        "sqlalchemy is the storage engine: only the store executes it and only "
        "table_spec declares tables with it:\n  " + "\n  ".join(offenders)
    )
