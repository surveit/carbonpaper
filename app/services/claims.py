"""Saving the claims a finished run made. The runtime computes them; this persists them."""
from __future__ import annotations

from collections.abc import Sequence

from app.core.frames import read_cell, read_frame_table
from app.core.run_status import StageStatus
from app.core.ids import ID
from app.models import Workflow
from app.models.claims import Claim, StageOutputCellCitation
from app.models.claims import CLAIMED_ROW
from app.models.run_manifest import StageRecord
from app.models.stages.aggregate import read_declared_claims
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
        declared = read_declared_claims(stage)
        if not declared or stage.id not in written:
            continue
        table = read_frame_table(run_dir / written[stage.id])
        saved += [
            _save_one(project_id, run_id, stage.id, claim.label, claim.column, table)
            for claim in declared
        ]
    return saved


def _save_one(
    project_id: ID, run_id: ID, stage_id: ID, label: str, column: str, table: object
) -> Claim:
    claim = Claim(
        project_id=project_id,
        label=label,
        citation=StageOutputCellCitation(
            run_id=run_id, stage_id=stage_id, row_ordinal=CLAIMED_ROW,
            column=column, value=read_cell(table, column, CLAIMED_ROW),
        ),
    )
    claim.save()
    return claim
