"""Which rows took one branch, and which frame they are rows of."""

from __future__ import annotations

from app.models.branch_analysis import BranchId, BranchReason, RowOrdinal
from app.models.schema import StageId
from app.runtime.branch_analysis.run_branches import (
    SOURCE_STAGE,
    WorkflowRunBranches,
)

# The arm whose rows are in the stage's INPUT frame, never its output.
DROPPED_ARM = "dropped"


def find_rows_that_took(run: WorkflowRunBranches, branch: BranchId
                        ) -> tuple[StageId, list[RowOrdinal]]:
    """Where a branch's rows live: a lost row is in the stage's INPUT frame."""
    fact = run.catalog[branch]
    if fact.reason is BranchReason.aggregate:
        return _find_group_members(run, branch)
    if fact.stage == SOURCE_STAGE:
        loader = branch.split("|", 1)[1]
        return loader, list(range(run.rows[loader]))
    arm = branch.split("|", 1)[1]
    if arm == DROPPED_ARM:
        return _find_lost_rows(run, fact.stage, arm)
    return fact.stage, [i for i, held in enumerate(run.paths[fact.stage])
                        if branch in held]

def _find_group_members(run: WorkflowRunBranches, branch: BranchId
                        ) -> tuple[StageId, list[RowOrdinal]]:
    for sid, rows in run.groups.items():
        members = sorted(row for row, held in rows.items() if branch in held)
        if members:
            return sid, members
    return run.catalog[branch].stage, []

def _find_lost_rows(run: WorkflowRunBranches, stage_id: StageId, arm: str
                    ) -> tuple[StageId, list[RowOrdinal]]:
    stage = run.stages[stage_id]
    parent = stage.inputs[0].id
    lineage = run.lineages[stage_id]
    if lineage is None:
        return parent, []
    kept = {p.row_ordinal for group in lineage.parents for p in group
            if p.stage_id == parent}
    return parent, [i for i in range(run.rows[parent]) if i not in kept]
