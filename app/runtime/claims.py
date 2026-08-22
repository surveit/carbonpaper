"""Refusing a claim whose stage did not reduce to one row. The runtime persists nothing."""
from __future__ import annotations

import pyarrow as pa

from app.models import WorkflowStage
from app.models.stages.aggregate import ProducedClaim, read_declared_claims

from .validation import Issue


def find_claim_row_issues(workflow_stage: WorkflowStage, table: pa.Table) -> list[Issue]:
    claims = read_declared_claims(workflow_stage.stage)
    if not claims or table.num_rows == 1:
        return []
    return [Issue(
        "error", None,
        f"{_name_them(claims)} read one cell, and this stage output "
        f"{table.num_rows:,} rows — a claim comes only from a stage that reduces to one",
    )]


def _name_them(claims: list[ProducedClaim]) -> str:
    return ", ".join(f"'{claim.label}'" for claim in claims)
