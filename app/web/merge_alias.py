"""A merge stage's groups, aliased into one node until a reader de-aliases it."""

from __future__ import annotations

from pathlib import Path

from app.core.frames import read_frame_table
from app.core.json_types import JsonScalar
from app.models.branch_analysis import (
    AliasedMerge,
    MergeGroup,
    RowOrdinal,
)
from app.models.schema import StageId
from app.models.stages.aggregate import AggregateStage
from app.runtime.branch_analysis import WorkflowRunBranches
from app.services.scope import find_merge_stage_ids

# One page of groups. A merge stage can have as many groups as its input has rows.
GROUP_PAGE = 100


def alias_the_merges(run_branches: WorkflowRunBranches,
                     reached: dict[StageId, set[RowOrdinal]],
                     resolved: StageId | None) -> dict[StageId, AliasedMerge]:
    """One node per merge stage the figure came through, standing in for its groups."""
    merged = find_merge_stage_ids(run_branches)
    return {stage_id: _describe(run_branches, stage_id, reached[stage_id])
            for stage_id in run_branches.ordered_stage_ids
            if stage_id != resolved and stage_id in reached and stage_id in merged}


def resolve_merge_groups(run_branches: WorkflowRunBranches, outputs: Path,
                         stage_id: StageId, reached: set[RowOrdinal],
                         offset: int) -> list[MergeGroup]:
    """One page of a merge stage's groups, the figure's own first."""
    counts = _count_rows_per_group(run_branches, stage_id)
    keys = _read_group_keys(run_branches, outputs, stage_id)
    ordered = sorted(counts, key=lambda ordinal: (ordinal not in reached, ordinal))
    return [MergeGroup(branch=f"{stage_id}|merged:{ordinal}", ordinal=ordinal,
                       keys=keys.get(ordinal, []), rows_count=counts[ordinal],
                       on_route=ordinal in reached)
            for ordinal in ordered[offset:offset + GROUP_PAGE]]


def count_merge_groups(run_branches: WorkflowRunBranches, stage_id: StageId) -> int:
    return len(_count_rows_per_group(run_branches, stage_id))


def _describe(run_branches: WorkflowRunBranches, stage_id: StageId,
              reached: set[RowOrdinal]) -> AliasedMerge:
    counts = _count_rows_per_group(run_branches, stage_id)
    merged_from = run_branches.stages[stage_id].inputs[0].id
    return AliasedMerge(
        stage_id=stage_id, rows_live_in_stage_id=merged_from,
        group_by=_read_group_by(run_branches, stage_id),
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


def _read_group_keys(run_branches: WorkflowRunBranches, outputs: Path,
                     stage_id: StageId) -> dict[RowOrdinal, list[JsonScalar]]:
    """The group_by cells of the merge stage's own frame — what each group stands for."""
    group_by = _read_group_by(run_branches, stage_id)
    if not group_by:
        return {}
    frame = read_frame_table(outputs / f"{stage_id}.parquet")
    named = [name for name in group_by if name in frame.column_names]
    columns = [frame.column(name).to_pylist() for name in named]
    return {ordinal: [column[ordinal] for column in columns]
            for ordinal in range(frame.num_rows)}
