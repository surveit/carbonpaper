"""Exceptions raised by the services layer."""
from __future__ import annotations

from pathlib import Path


class WorkflowLoadError(Exception):
    """A stored workflow failed validation; `issues` lists every problem found.
    `source` names where the workflow was read from — a compiled/ directory, or
    a version document in the store."""

    def __init__(self, source: Path | str, issues: list[str]):
        self.issues = issues
        super().__init__(
            f"{source}: {len(issues)} validation issue(s):\n  "
            + "\n  ".join(issues)
        )


class InputProfilerNotConfiguredError(Exception):
    """Observation was asked for before a composition root injected the profiler."""

    def __init__(self) -> None:
        super().__init__(
            "no input profiler is configured — a composition root allowed to "
            "import app.runtime (app.web, app.mcp) must call "
            "app.services.observation.set_input_profiler(...) before observed "
            "values can be served"
        )
