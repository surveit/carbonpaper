"""Workflow-run execution status: the per-stage and overall-run `status`
values a run's manifest records (see app.runtime.runner).

Both are `enum.StrEnum`, not the `class X(str, Enum)` pattern used for the
workflow-contract vocabularies in app.core.models (StageType, ConnectorKind,
...). That distinction matters here specifically: these values are rendered
as bare strings on paths only StrEnum gets right — Jinja builds CSS classes
with `status-{{ status }}`, the run-page poller reads `status` straight off
the JSON API, and the manifest itself is JSON on disk. A `class X(str, Enum)`
member renders `str()`/an f-string as `"ClassName.MEMBER"`; an `enum.StrEnum`
member renders as its bare value ("ok"), matches `== "ok"`, and
`json.dumps`-serialises as `"ok"` with no `default=str` needed.
"""
from __future__ import annotations

import enum


class StageStatus(enum.StrEnum):
    """One stage's outcome for a single run, as recorded in
    `manifest["stages"][i]["status"]`."""

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
