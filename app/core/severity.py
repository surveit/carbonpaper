"""The two words an issue is graded in, wherever it was raised."""
from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    """One vocabulary: a run's validation issues and a workflow's compiler warnings
    share a table."""

    error = "error"
    warning = "warning"
