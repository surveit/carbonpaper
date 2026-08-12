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


class UploadTooLargeError(Exception):
    """Over the per-file ceiling or the project's upload quota; the message says which."""


class SpecMigrationRefused(ValueError):
    """A stored stage payload whose signature the read-side upgrade cannot determine."""
