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
