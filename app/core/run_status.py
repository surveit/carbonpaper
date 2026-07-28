"""Workflow-run enums: the per-stage and overall-run `status` a run's manifest records.

`enum.StrEnum`, deliberately NOT the `class X(str, Enum)` pattern used elsewhere:
these values are interpolated as bare strings (Jinja `status-{{ status }}`, JSON
manifest), where `class X(str, Enum)` would render `"ClassName.MEMBER"`.
"""
from __future__ import annotations

import enum


class StageStatus(enum.StrEnum):
    """One stage's outcome for a single run, as recorded in
    `manifest["stage_records"][i]["status"]`."""

    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    VALIDATION_WARNINGS = "validation_warnings"
    ERROR = "error"
    AWAITING_REVIEW = "awaiting_review"
    CANCELLED = "cancelled"


class RunStatus(enum.StrEnum):
    """A run's overall outcome across all its stages, as recorded in
    `manifest["status"]`. Distinct from StageStatus: a run aggregates to
    "errors"/"warnings" (plural) where a stage reports "error"/
    "validation_warnings"."""

    RUNNING = "running"
    OK = "ok"
    WARNINGS = "warnings"
    ERRORS = "errors"
    AWAITING_REVIEW = "awaiting_review"
    CANCELLED = "cancelled"
