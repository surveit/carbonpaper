"""Architecture: the workflow-test seam never mints a production run.

app/services/workflow_test.py drives a non-production (workflow) test through the
shared execution engine in app.runtime.executor (run_subset / _execute_stages),
NOT through app.runtime.runner's production run-lifecycle entry points — those
create a run record under runs/, which a workflow test must never do.
app.services.run is the ONE service module that may reach app.runtime.runner (the
production seam), so this rule scopes to workflow_test.py alone.

The import-graph seal ("app.runtime.runner imported only by app.services.run",
pyproject [tool.importlinter]) already forbids every other module from importing
it; this colocated AST check is the belt-and-suspenders that keeps the workflow-test
seam honest right where the workflow-test code lives.
"""
from __future__ import annotations

from arch import find_governed_files, find_production_run_imports


def test_workflow_test_never_imports_production_runner() -> None:
    workflow_test = [p for p in find_governed_files(__file__) if p.name == "workflow_test.py"]
    assert workflow_test, "expected app/services/workflow_test.py in this arch test's scope"
    offenders = find_production_run_imports(workflow_test)
    assert not offenders, (
        "app/services/workflow_test.py must reach the execution engine through "
        "app.runtime.executor (run_subset / _execute_stages), never the "
        "production run-lifecycle entry points in app.runtime.runner — importing "
        "those would let a workflow test mint a production run under runs/. Offending "
        "files:\n  " + "\n  ".join(offenders)
    )
