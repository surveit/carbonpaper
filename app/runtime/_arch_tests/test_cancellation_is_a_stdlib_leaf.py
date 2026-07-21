"""Architecture: the cancel mailbox is a stdlib-only leaf.

``app/runtime/cancellation.py`` holds the per-run cancel mailbox
(request_cancel drops a message, consume_cancel pops it). It is keyed on a
run's logical ``(project, run_id)`` identity and must know nothing about how or
where a run is stored — not the persistence layer, not the web layer, not any
other app module. That independence is what keeps cancellation a pure signal
rather than something entangled with run state.

Enforced as a stdlib-only allowlist: every import in the module is a standard-
library module, and there are no relative imports. An allowlist (rather than a
``forbidden`` import-linter contract enumerating the modules to deny) is
self-maintaining — a new sibling under ``app/runtime/`` needs no change here —
and it catches an in-project import written either absolutely
(``from app.runtime.runner import x``) or relatively (``from .runner import
x``) alike.
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
