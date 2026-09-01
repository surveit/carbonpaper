"""Publishing what a run produced for each workflow output its stages declared."""
from __future__ import annotations

import pyarrow as pa

from app.core.frames import read_cell
from app.core.ids import ID
from app.models import WorkflowStage
from app.models.claims import (
    RowsRectangle,
    StageOutputCellCitation,
    StageOutputTableCitation,
)
from app.models.records.workflow_output import WorkflowOutput
from app.models.stages.stage_base import WorkflowFigureRule, WorkflowTableRule

from .context import RunIdentity
from .validation import Issue

# A figure is one cell, so the row it reads is the only row there is.
PUBLISHED_ROW = 0


def find_workflow_output_issues(
    workflow_stage: WorkflowStage, table: pa.Table
) -> list[Issue]:
    stage = workflow_stage.stage
    return _find_figure_row_issues(stage.list_published_figures(), table) + [
        issue
        for declared in stage.list_published_tables()
        for issue in _find_table_column_issues(declared, table)
    ]


def save_workflow_outputs(
    workflow_stage: WorkflowStage, table: pa.Table, identity: RunIdentity
) -> list[WorkflowOutput]:
    stage = workflow_stage.stage
    published = [
        _publish_figure(figure, workflow_stage.id, table, identity)
        for figure in stage.list_published_figures()
    ] + [
        _publish_table(declared, workflow_stage.id, table, identity)
        for declared in stage.list_published_tables()
    ]
    for output in published:
        output.save()
    return published


def read_published_columns(declared: WorkflowTableRule, table: pa.Table) -> list[str]:
    return list(declared.columns) if declared.columns else list(table.column_names)


def _find_figure_row_issues(
    figures: list[WorkflowFigureRule], table: pa.Table
) -> list[Issue]:
    if not figures or table.num_rows == 1:
        return []
    columns = ", ".join(f"`{figure.column}`" for figure in figures)
    return [Issue(
        "error", None,
        f"{columns} are published as workflow figures, which are one cell each, and "
        f"this stage output {table.num_rows:,} rows — reduce it to one first",
    )]


def _find_table_column_issues(
    declared: WorkflowTableRule, table: pa.Table
) -> list[Issue]:
    missing = [c for c in declared.columns or [] if c not in table.column_names]
    if not missing:
        return []
    return [Issue(
        "error", None,
        f"workflow output '{declared.slug}' publishes "
        f"{', '.join(f'`{c}`' for c in missing)}, which this stage does not output — "
        f"it has {', '.join(f'`{c}`' for c in table.column_names)}",
    )]


def _publish_figure(
    figure: WorkflowFigureRule, stage_id: ID, table: pa.Table, identity: RunIdentity
) -> WorkflowOutput:
    return WorkflowOutput(
        slug=figure.slug, label=figure.label, primary=figure.primary, shape_id=figure.shape_id,
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
            run_id=identity.run_id, stage_id=stage_id,
            rectangle=RowsRectangle(
                row_start=0, row_end=table.num_rows,
                columns=read_published_columns(declared, table),
            ),
        ),
    )
