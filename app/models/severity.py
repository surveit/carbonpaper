"""The severity a person reads on an issue, wherever it was raised.

`enum.StrEnum` for the reason `app.core.run_status` gives: this value is
interpolated bare (Jinja `sev-{{ severity }}`, JSON), where `class X(str, Enum)`
would render the member's repr instead of its value.
"""
from __future__ import annotations

import enum


class UserFacingErrorSeverity(enum.StrEnum):
    """One vocabulary: a run's issues and a workflow's compiler warnings share a table."""

    error = "error"
    warning = "warning"
