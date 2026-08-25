"""A merge stage's groups, aliased into one node until a reader expands them."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.core.frames import read_frame_table
from app.core.json_types import JsonScalar
from app.models.branch_analysis import BranchId, BranchReason, RowOrdinal
from app.models.schema import StageId
from app.models.stages.aggregate import AggregateStage
from app.runtime.branch_analysis import WorkflowRunBranches
from app.services.scope import find_merge_stage_ids


class AliasedMerge(BaseModel):
    """A merge stage drawn as one node, its groups left unresolved."""

    stage_id: StageId
    group_by: list[str]
    groups_count: int
    rows_count: int
    on_route_groups_count: int
    on_route_rows_count: int


def find_branches_that_tell_rows_apart(run_branches: WorkflowRunBranches,
                                       on_route: set[StageId],
                                       resolved: set[StageId]) -> set[BranchId]:
    """On the route — and, for a merge, at a stage this reader has expanded."""
    return {branch_id
            for branch_id, option in run_branches.branch_options.items()
            if option.stage_id in on_route
            and (option.reason is not BranchReason.merge
                 or option.stage_id in resolved)}


def alias_the_merges(run_branches: WorkflowRunBranches,
                     reached: dict[StageId, set[RowOrdinal]],
                     resolved: set[StageId]) -> dict[StageId, AliasedMerge]:
    """One node per merge stage the figure came through that nothing resolved."""
    merged = find_merge_stage_ids(run_branches)
    return {stage_id: _describe(run_branches, stage_id, reached[stage_id])
            for stage_id in run_branches.ordered_stage_ids
            if stage_id in reached and stage_id in merged
            and stage_id not in resolved}


def name_the_groups(run_branches: WorkflowRunBranches, outputs: Path,
                    stage_id: StageId) -> dict[RowOrdinal, str]:
    """A group stands for its group_by values, never for the ordinal it landed on."""
    group_by = _read_group_by(run_branches, stage_id)
    if not group_by:
        return {}
    frame = read_frame_table(outputs / f"{stage_id}.parquet")
    named = [name for name in group_by if name in frame.column_names]
    columns = [frame.column(name).to_pylist() for name in named]
    return {ordinal: " · ".join(f"{name} = {_plain(column[ordinal])}"
                                for name, column in zip(named, columns))
            for ordinal in range(frame.num_rows)}


def _describe(run_branches: WorkflowRunBranches, stage_id: StageId,
              reached: set[RowOrdinal]) -> AliasedMerge:
    counts = _count_rows_per_group(run_branches, stage_id)
    return AliasedMerge(
        stage_id=stage_id, group_by=_read_group_by(run_branches, stage_id),
        groups_count=len(counts), rows_count=sum(counts.values()),
        on_route_groups_count=len(reached & set(counts)),
        on_route_rows_count=sum(counts[o] for o in reached if o in counts))


def _count_rows_per_group(run_branches: WorkflowRunBranches,
                          stage_id: StageId) -> dict[RowOrdinal, int]:
    lineage = run_branches.lineages.get(stage_id)
    if lineage is None:
        return {}
    return {ordinal: len(parents)
            for ordinal, parents in enumerate(lineage.parents) if parents}


def _read_group_by(run_branches: WorkflowRunBranches, stage_id: StageId) -> list[str]:
    stage = run_branches.stages[stage_id].stage
    return list(stage.aggregate.group_by) if isinstance(stage, AggregateStage) else []


def _plain(value: object) -> JsonScalar:
    # A group key names a group, so 2024 stays 2024 and never becomes 2,024.
    return "(empty)" if value is None else str(value)
