"""Which rows took one branch, and which frame they are rows of."""

from __future__ import annotations

from app.models.branch_analysis import (
    BranchId,
    BranchOption,
    BranchReason,
    BranchRole,
    RowOrdinal,
)
from app.models.schema import StageId
from app.runtime.branch_analysis.run_branches import (
    MERGE_EDGE,
    WorkflowRunBranches,
)


def find_rows_that_took(run_branches: WorkflowRunBranches, branch_id: BranchId
                        ) -> tuple[StageId, list[RowOrdinal]]:
    """A branch's rows are rows of `rows_live_in_stage_id`, never always its own stage."""
    branch = run_branches.branch_options[branch_id]
    if branch.reason is BranchReason.merge:
        return branch.rows_live_in_stage_id, _find_merged_rows(run_branches, branch)
    if branch.reason is BranchReason.load:
        return branch.stage_id, list(range(run_branches.row_counts[branch.stage_id]))
    if branch.role is BranchRole.removes:
        return branch.rows_live_in_stage_id, _find_removed_rows(run_branches, branch)
    return branch.stage_id, [
        row for row, path in enumerate(run_branches.branch_paths[branch.stage_id])
        if branch_id in path]


def _find_merged_rows(run_branches: WorkflowRunBranches,
                      branch: BranchOption) -> list[RowOrdinal]:
    merged = run_branches.merges_per_row.get(branch.rows_live_in_stage_id, {})
    return sorted(row for row, held in merged.items() if branch.id in held)


def _find_removed_rows(run_branches: WorkflowRunBranches,
                       branch: BranchOption) -> list[RowOrdinal]:
    """Removed rows are the input's rows no output row reaches."""
    lineage = run_branches.lineages[branch.stage_id]
    if lineage is None:
        return []
    input_stage_id = branch.rows_live_in_stage_id
    reached = {p.row_ordinal for entry in lineage.parents for p in entry
               if p.stage_id == input_stage_id and p.kind != MERGE_EDGE}
    return [row for row in range(run_branches.row_counts[input_stage_id])
            if row not in reached]
