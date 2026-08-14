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


class FileHeldByAnotherProject(Exception):
    """A second project claiming a file the store already recorded against a first."""

    def __init__(self, *, file_id: str, held_by: str, claimed_by: str) -> None:
        self.file_id, self.held_by, self.claimed_by = file_id, held_by, claimed_by
        super().__init__(
            f"file {file_id!r} is held by project '{held_by}' and cannot also be held by "
            f"'{claimed_by}' — upload the bytes to '{claimed_by}', which stores no second copy")


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
