"""What a run states: the cell a stage named, minted here and persisted by a caller."""
from __future__ import annotations

import pyarrow as pa

from app.core.frames import read_cell
from app.models import WorkflowStage
from app.models.claims import Claim, StageOutputCellCitation

from .context import RunIdentity
from .validation import Issue

# A claim reads one cell, and the stage that states one is refused unless it has just the one row.
CLAIMED_ROW = 0


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


def mint_stage_claims(
    workflow_stage: WorkflowStage, table: pa.Table, identity: RunIdentity
) -> list[Claim]:
    return [
        Claim(
            shape_id=stated.shape_id,
            citation=StageOutputCellCitation(
                run_id=identity.run_id, stage_id=workflow_stage.id,
                row_ordinal=CLAIMED_ROW, column=stated.column,
                value=read_cell(table, stated.column, CLAIMED_ROW),
            ),
        )
        for stated in (workflow_stage.stage.claims or [])
    ]
