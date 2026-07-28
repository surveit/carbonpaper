"""Architecture: the cancel mailbox is a stdlib-only leaf.

Enforced as a stdlib allowlist rather than a `forbidden` contract, so a new
sibling under `app/runtime/` needs no change here and an in-project import
written absolutely or relatively is caught alike.
"""
from __future__ import annotations

from pathlib import Path

from arch import check_imports_are_stdlib_only


def test_cancellation_imports_only_stdlib() -> None:
    cancellation = Path(__file__).resolve().parents[1] / "cancellation.py"
    offenders = check_imports_are_stdlib_only([cancellation])
    assert not offenders, (
        "app/runtime/cancellation.py must be a stdlib-only leaf — the cancel "
        "mailbox stays independent of the persistence model and every other app "
        "module. Offending imports:\n  " + "\n  ".join(offenders)
    )
