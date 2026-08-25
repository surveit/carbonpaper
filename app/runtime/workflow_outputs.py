"""Publishing what a run produced for each workflow output its stages declared."""
from __future__ import annotations

import pyarrow as pa

from app.core.frames import read_cell
from app.core.ids import ID
from app.models import WorkflowStage
from app.models.claims import StageOutputCellCitation, StageOutputTableCitation
from app.models.records.workflow_output import WorkflowOutput
from app.models.stages.stage_base import WorkflowFigureRule, WorkflowTableRule

from .context import RunIdentity
from .validation import Issue

# A figure is one cell, so the row it reads is the only row there is.
PUBLISHED_ROW = 0


def find_output_row_issues(workflow_stage: WorkflowStage, table: pa.Table) -> list[Issue]:
    figures = workflow_stage.stage.list_published_figures()
    if not figures or table.num_rows == 1:
        return []
    columns = ", ".join(f"`{figure.column}`" for figure in figures)
    return [Issue(
        "error", None,
        f"{columns} are published as workflow figures, which are one cell each, and "
        f"this stage output {table.num_rows:,} rows — reduce it to one first",
    )]


def save_workflow_outputs(
    workflow_stage: WorkflowStage, table: pa.Table, identity: RunIdentity
) -> list[WorkflowOutput]:
    published = [
        _publish_figure(figure, workflow_stage.id, table, identity)
        for figure in workflow_stage.stage.list_published_figures()
    ]
    declared_table = workflow_stage.stage.find_published_table()
    if declared_table is not None:
        published.append(_publish_table(declared_table, workflow_stage.id, table, identity))
    for output in published:
        output.save()
    return published


def _publish_figure(
    figure: WorkflowFigureRule, stage_id: ID, table: pa.Table, identity: RunIdentity
) -> WorkflowOutput:
    return WorkflowOutput(
        slug=figure.slug, label=figure.label, primary=figure.primary,
        citation=StageOutputCellCitation(
            run_id=identity.run_id, stage_id=stage_id,
            row_ordinal=PUBLISHED_ROW, column=figure.column,
            value=read_cell(table, figure.column, PUBLISHED_ROW),
        ),
    )


def _publish_table(
    declared: WorkflowTableRule, stage_id: ID, table: pa.Table, identity: RunIdentity
) -> WorkflowOutput:
    return WorkflowOutput(
        slug=declared.slug, label=declared.label, primary=declared.primary,
        citation=StageOutputTableCitation(
            run_id=identity.run_id, stage_id=stage_id, row_count=table.num_rows,
        ),
    )
