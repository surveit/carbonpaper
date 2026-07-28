"""Architecture: the seed platform reaches the store through services, not directly.

No file under app/seeds/ imports app.core.persistence except bootstrap.py — the one
composition-root exception. Enforced as an allowlist, not a forbidden import-linter
contract.
"""
from __future__ import annotations

from arch import check_no_import, find_governed_files


def test_seed_reaches_the_store_only_through_services() -> None:
    offenders = check_no_import(
        find_governed_files(__file__),
        "app.core.persistence",
        allow={"app/seeds/bootstrap.py"},
    )
    assert not offenders, (
        "app/seeds must reach the document store through app.services "
        "(project.import_project), not app.core.persistence directly — only "
        "bootstrap.py configures the store, like app.main. Offending files:\n  "
        + "\n  ".join(offenders)
    )
