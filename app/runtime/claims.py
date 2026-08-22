"""Refusing a claim whose stage did not reduce to the one row a scalar claim reads."""
from __future__ import annotations

import pyarrow as pa

from app.models import WorkflowStage

from .validation import Issue


def find_claim_row_issues(workflow_stage: WorkflowStage, table: pa.Table) -> list[Issue]:
    claims = workflow_stage.stage.claims
    if not claims or table.num_rows == 1:
        return []
    columns = ", ".join(f"`{stated.column}`" for stated in claims)
    return [Issue(
        "error", None,
        f"{columns} state a claim, which reads one cell, and this stage output "
        f"{table.num_rows:,} rows — reduce it to one first",
    )]
