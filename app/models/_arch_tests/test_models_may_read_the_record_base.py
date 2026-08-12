"""Architecture: a contract may import the record base, never the storage engine.

The import-linter contract states the prohibition; the second test states what makes the
permission safe — importing PersistedModel beside app.models loads no database driver.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from arch import check_no_import, find_governed_files

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE = "app.core.sqlite_store"
# A fresh interpreter: sqlite3 is already loaded inside a pytest process, so the question
# "did this import bring the engine along?" can only be asked of a process that ran nothing else.
_PROBE = (
    "import sys\n"
    "import app.models\n"
    "from app.core.persistence import PersistedModel\n"
    "print('sqlite3' in sys.modules)\n"
)


def test_no_contract_imports_the_storage_engine() -> None:
    offenders = check_no_import(find_governed_files(__file__), _ENGINE, allow=set())
    assert not offenders, (
        f"app/models holds validation-only contracts, so none may name {_ENGINE}; "
        "a record persists through app.core.persistence, which carries no engine:\n  "
        + "\n  ".join(offenders)
    )


def test_importing_the_record_base_beside_the_contracts_loads_no_engine() -> None:
    probe = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert probe.returncode == 0, (
        "app.models must be importable alongside app.core.persistence — the record base "
        f"is what a record embedding a contract would import:\n{probe.stderr}"
    )
    assert probe.stdout.strip() == "False", (
        "importing app.models beside PersistedModel pulled sqlite3 in, which puts the "
        f"engine behind the contracts: either a contract names {_ENGINE} (the test above "
        "says which) or app/core/persistence.py now reaches it"
    )
