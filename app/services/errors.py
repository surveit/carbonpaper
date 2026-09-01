"""Exceptions raised by the services layer."""
from __future__ import annotations

from pathlib import Path


class WorkflowLoadError(Exception):
    def __init__(self, source: Path | str, issues: list[str]):
        self.issues = issues
        super().__init__(
            f"{source}: {len(issues)} validation issue(s):\n  "
            + "\n  ".join(issues)
        )


class SpecMigrationRefused(ValueError):
    """A stored stage payload whose signature the read-side upgrade cannot determine."""


class CacheArchiveRejected(Exception):
    """A stage-cache export this workspace could not read a single entry of."""


class ProjectArchiveRejected(Exception):
    """A project archive this workspace could not read a project out of."""
