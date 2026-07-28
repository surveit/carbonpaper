"""Architecture: the workflow-test seam never mints a production run.

Scoped to workflow_test.py alone — app.services.run is the ONE service module
that may reach app.runtime.runner.
"""
from __future__ import annotations

from arch import find_governed_files, find_production_run_imports


def test_workflow_test_never_imports_production_runner() -> None:
    workflow_test = [p for p in find_governed_files(__file__) if p.name == "workflow_test.py"]
    assert workflow_test, "expected app/services/workflow_test.py in this arch test's scope"
    offenders = find_production_run_imports(workflow_test)
    assert not offenders, (
        "app/services/workflow_test.py must reach the execution engine through "
        "app.runtime.executor (run_subset / execute_stages), never the "
        "production run-lifecycle entry points in app.runtime.runner — importing "
        "those would let a workflow test mint a production run under runs/. Offending "
        "files:\n  " + "\n  ".join(offenders)
    )
