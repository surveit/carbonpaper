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


class ClaimShapeWriteRefused(ValueError):
    """The WRITE is refused, whole: a bad entry takes the batch with it."""

    def __init__(self, refusals: list[str]) -> None:
        super().__init__("; ".join(refusals))
        self.refusals = refusals


class ClaimRefused(ValueError):
    """Nothing is written unless the claim may stand."""

    def __init__(self, refusals: list[str]) -> None:
        super().__init__("; ".join(refusals))
        self.refusals = refusals
