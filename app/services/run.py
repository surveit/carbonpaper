"""Production run seam: the one service module allowed to drive
app.runtime.runner's production run-lifecycle entry points (prepare_run /
run_prepared / resume_run / resolve_version_id).

Filled in by the run-service task; kept as a resolvable module here so the
import-linter contracts that name it (allowed importer of app.runtime, sole
importer of app.runtime.runner) resolve."""
from __future__ import annotations
