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


# The stage statuses whose output frame holds what the stage promised. An `error`
# stage also wrote a frame, but its untouched columns are nulls rather than results.
FINISHED_STAGE_STATUSES = (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    OK = "ok"
    WARNINGS = "warnings"
    ERRORS = "errors"
    AWAITING_REVIEW = "awaiting_review"
    CANCELLED = "cancelled"
