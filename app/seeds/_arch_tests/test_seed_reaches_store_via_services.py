"""Architecture: the seed platform reaches the store through services, not directly.

app/seeds loads committed example projects into the workspace by calling
app.services (project.import_project), which owns the document store. The seed
LOGIC never imports app.core.persistence itself — only the CLI bootstrap
(bootstrap.py) configures the store, exactly as app.main does, so a store-free
`python -m app.seeds` process doesn't crash.

Enforced as an allowlist (like the cancellation stdlib-leaf and the sqlite seal),
NOT a forbidden import-linter contract: no file under app/seeds/ imports
app.core.persistence except bootstrap.py — the one composition-root exception.
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
