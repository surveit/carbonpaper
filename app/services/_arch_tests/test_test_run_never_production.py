"""Architecture: the test-run seam never mints a production run.

app/services/test_run.py drives a non-production (test) run through the shared
execution engine in app.runtime.executor (run_subset / _execute_stages), NOT
through app.runtime.runner's production run-lifecycle entry points — those create
a run record under runs/, which a test run must never do. app.services.run is
the ONE service module that may reach app.runtime.runner (the production seam),
so this rule scopes to test_run.py alone.

The import-graph seal ("app.runtime.runner imported only by app.services.run",
pyproject [tool.importlinter]) already forbids every other module from importing
it; this colocated AST check is the belt-and-suspenders that keeps the test-run
seam honest right where the test-run code lives.
"""
from __future__ import annotations

from arch import find_governed_files, find_production_run_imports


def test_test_run_never_imports_production_runner() -> None:
    test_run = [p for p in find_governed_files(__file__) if p.name == "test_run.py"]
    assert test_run, "expected app/services/test_run.py in this arch test's scope"
    offenders = find_production_run_imports(test_run)
    assert not offenders, (
        "app/services/test_run.py must reach the execution engine through "
        "app.runtime.executor (run_subset / _execute_stages), never the "
        "production run-lifecycle entry points in app.runtime.runner — importing "
        "those would let a test run mint a production run under runs/. Offending "
        "files:\n  " + "\n  ".join(offenders)
    )
