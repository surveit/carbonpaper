"""The two words an issue is graded in, wherever it was raised.

`enum.StrEnum` for the reason `app.core.run_status` gives: this value is
interpolated bare (Jinja `sev-{{ severity }}`, JSON), where `class X(str, Enum)`
would render `"Severity.error"`.
"""
from __future__ import annotations

import enum


class Severity(enum.StrEnum):
    """One vocabulary: a run's validation issues and a workflow's compiler warnings
    share a table."""

    error = "error"
    warning = "warning"
