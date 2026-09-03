"""Workflow-run enums: the per-stage and overall-run `status` a run's manifest records.

`enum.StrEnum`, deliberately NOT the `class X(str, Enum)` pattern used elsewhere:
these values are interpolated as bare strings (Jinja `status-{{ status }}`, JSON
manifest), where `class X(str, Enum)` would render `"ClassName.MEMBER"`.
"""
from __future__ import annotations

import enum


class StageStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    VALIDATION_WARNINGS = "validation_warnings"
    ERROR = "error"
    AWAITING_REVIEW = "awaiting_review"
    CANCELLED = "cancelled"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    OK = "ok"
    WARNINGS = "warnings"
    ERRORS = "errors"
    AWAITING_REVIEW = "awaiting_review"
    CANCELLED = "cancelled"


def is_run_still_going(status: str) -> bool:
    """A run that may still record more stages; nothing worked out over it can be kept."""
    return status in (RunStatus.RUNNING, RunStatus.AWAITING_REVIEW)
