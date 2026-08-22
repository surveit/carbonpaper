"""Saving the claims a finished run made. The runtime computes them; this persists them."""
from __future__ import annotations

from collections.abc import Sequence

from app.core.frames import read_cell, read_frame_table
from app.core.run_status import StageStatus
from app.core.ids import ID
from app.models import Workflow
from app.models.claims import Claim, StageOutputCellCitation
from app.models.run_manifest import StageRecord
from app.services.workspace import resolve_run_dir


def save_run_claims(
    project_id: ID, run_id: ID, workflow: Workflow, records: Sequence[StageRecord]
) -> list[Claim]:
    """Reads the run's own outputs, so a resumed run re-states what it already had."""
    run_dir = resolve_run_dir(project_id, run_id)
    # An errored stage still wrote its frame, so status decides, not the file.
    written = {
        record.stage_id: record.output_path
        for record in records
        if record.output_path and record.status != StageStatus.ERROR
    }
    saved: list[Claim] = []
    for stage in workflow.stages:
        if not stage.claims or stage.id not in written:
            continue
        table = read_frame_table(run_dir / written[stage.id])
        assert table.num_rows == 1, f"{stage.id}: the runtime lets only one row through"
        for stated in stage.claims:
            claim = Claim(
                shape_id=stated.shape_id,
                citation=StageOutputCellCitation(
                    run_id=run_id, stage_id=stage.id, row_ordinal=0,
                    column=stated.column, value=read_cell(table, stated.column, 0),
                ),
            )
            claim.save()
            saved.append(claim)
    return saved
