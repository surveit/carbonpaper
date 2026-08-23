"""What a run published: a declared column's value, kept with the run that produced it."""
from __future__ import annotations

from typing import ClassVar

import pyarrow as pa

from app.core.frames import read_cell
from app.core.ids import ID
from app.core.persistence import JsonScalar, PersistedModel, PersistenceScope
from app.models import WorkflowStage

from .context import RunIdentity
from .validation import Issue

# A scalar output is one cell, so the row it reads is the only row there is.
PUBLISHED_ROW = 0


class WorkflowOutput(PersistedModel):
    collection: ClassVar[str] = "workflow_output"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    slug: str
    label: str
    value: JsonScalar
    # Where to look, and what a reader will see there.
    run_id: ID
    stage_id: ID
    column: str
    row_ordinal: int


def find_output_row_issues(workflow_stage: WorkflowStage, table: pa.Table) -> list[Issue]:
    declared = workflow_stage.stage.outputs
    if not declared or table.num_rows == 1:
        return []
    columns = ", ".join(f"`{output.column}`" for output in declared)
    return [Issue(
        "error", None,
        f"{columns} are published as results, which are one cell each, and this stage "
        f"output {table.num_rows:,} rows — reduce it to one first",
    )]


def save_stage_outputs(
    workflow_stage: WorkflowStage, table: pa.Table, identity: RunIdentity
) -> list[WorkflowOutput]:
    saved = [
        WorkflowOutput(
            slug=output.slug, label=output.label,
            value=read_cell(table, output.column, PUBLISHED_ROW),
            run_id=identity.run_id, stage_id=workflow_stage.id,
            column=output.column, row_ordinal=PUBLISHED_ROW,
        )
        for output in (workflow_stage.stage.outputs or [])
    ]
    for output in saved:
        output.save()
    return saved
