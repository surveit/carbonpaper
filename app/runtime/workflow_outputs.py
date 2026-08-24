"""Publishing what a run produced for each workflow output its stages declared."""
from __future__ import annotations

import pyarrow as pa

from app.core.frames import read_cell
from app.models import WorkflowStage
from app.models.claims import StageOutputCellCitation
from app.models.records.workflow_output import WorkflowOutput

from .context import RunIdentity
from .validation import Issue

# A workflow output is one cell, so the row it reads is the only row there is.
PUBLISHED_ROW = 0


def find_output_row_issues(workflow_stage: WorkflowStage, table: pa.Table) -> list[Issue]:
    declared = workflow_stage.stage.workflow_outputs
    if not declared or table.num_rows == 1:
        return []
    columns = ", ".join(f"`{output.column}`" for output in declared)
    return [Issue(
        "error", None,
        f"{columns} are published as workflow outputs, which are one cell each, and "
        f"this stage output {table.num_rows:,} rows — reduce it to one first",
    )]


def save_workflow_outputs(
    workflow_stage: WorkflowStage, table: pa.Table, identity: RunIdentity
) -> list[WorkflowOutput]:
    published = [
        WorkflowOutput(
            slug=output.slug, label=output.label, primary=output.primary,
            citation=StageOutputCellCitation(
                run_id=identity.run_id, stage_id=workflow_stage.id,
                row_ordinal=PUBLISHED_ROW, column=output.column,
                value=read_cell(table, output.column, PUBLISHED_ROW),
            ),
        )
        for output in (workflow_stage.stage.workflow_outputs or [])
    ]
    for output in published:
        output.save()
    return published
