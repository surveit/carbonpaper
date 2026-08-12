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


class FileNotStoredError(Exception):
    """A file id the project has no stored bytes for."""


class FileOverCeiling(Exception):
    """Carries the numbers, not a sentence — a surface writes the sentence."""

    def __init__(self, *, ceiling: int) -> None:
        self.ceiling = ceiling
        super().__init__(f"file over the {ceiling}-byte ceiling")


class StoreOverQuota(Exception):
    """Carries the numbers, not a sentence — a surface writes the sentence."""

    def __init__(self, *, used: int, quota: int, sent: int, root: Path) -> None:
        self.used, self.quota, self.sent, self.root = used, quota, sent, root
        super().__init__(f"store would reach {used} bytes, over the {quota}-byte limit")


class SpecMigrationRefused(ValueError):
    """A stored stage payload whose signature the read-side upgrade cannot determine."""
