"""Smoke-run seam: a non-production run reaches the shared execution engine
through app.runtime.executor (run_subset / _execute_stages), never the
production run-lifecycle entry points in app.runtime.runner — so a smoke run can
never mint a production run record under runs/.

Kept as a resolvable, runner-free module so the import-linter contract that lists
it as an allowed importer of app.runtime resolves, and the colocated arch test
that forbids it from importing app.runtime.runner has a file to govern."""
from __future__ import annotations
