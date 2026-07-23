"""Architecture: the smoke-run seam never mints a production run.

app/services/smoke.py drives a non-production (smoke) run through the shared
execution engine in app.runtime.executor (run_subset / _execute_stages), NOT
through app.runtime.runner's production run-lifecycle entry points — those create
a run record under runs/, which a smoke run must never do. app.services.run is
the ONE service module that may reach app.runtime.runner (the production seam),
so this rule scopes to smoke.py alone.

The import-graph seal ("app.runtime.runner imported only by app.services.run",
pyproject [tool.importlinter]) already forbids every other module from importing
it; this colocated AST check is the belt-and-suspenders that keeps the smoke seam
honest right where the smoke code lives.
"""
from __future__ import annotations

from arch import find_governed_files, find_production_run_imports


def test_smoke_run_never_imports_production_runner() -> None:
    smoke = [p for p in find_governed_files(__file__) if p.name == "smoke.py"]
    assert smoke, "expected app/services/smoke.py in this arch test's scope"
    offenders = find_production_run_imports(smoke)
    assert not offenders, (
        "app/services/smoke.py must reach the execution engine through "
        "app.runtime.executor (run_subset / _execute_stages), never the "
        "production run-lifecycle entry points in app.runtime.runner — importing "
        "those would let a smoke run mint a production run under runs/. Offending "
        "files:\n  " + "\n  ".join(offenders)
    )
