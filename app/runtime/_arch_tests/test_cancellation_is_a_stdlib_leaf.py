"""Architecture: cancellation identity is logical, not persistence-derived.

``app/runtime/cancellation.py`` is the process-global registry of cancel-
requested ``(project, run_id)`` keys, polled by the run thread and written by
the web thread. It must import nothing from ``app.`` (stdlib only) — if it
reached into any other app module, it could key off (or leak knowledge of)
the run directory / persistence layout, and cancellation would stop being
independent of how a run happens to be stored. Scope is this one file, not
the rest of app/runtime (which is free to import app.* normally).
"""
from __future__ import annotations

from pathlib import Path

from arch import check_no_import


def test_cancellation_module_imports_nothing_from_app() -> None:
    cancellation = Path(__file__).resolve().parents[1] / "cancellation.py"
    offenders = check_no_import([cancellation], "app", allow=set())
    assert not offenders, (
        "app/runtime/cancellation.py must stay a stdlib-only leaf so the "
        "cancel registry never depends on the persistence layout:\n  "
        + "\n  ".join(offenders)
    )
