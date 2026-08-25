"""Which rows produced a cited cell, over a run's branches. docs/branch-analysis.md."""

from __future__ import annotations

from typing import Sequence

from app.models.branch_analysis import (
    BranchReason,
    FrameScale,
    RowOrdinal,
    RowRef,
    RowSet,
)
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.runtime.branch_analysis.run_branches import (
    MERGE_EDGE,
    WorkflowRunBranches,
    find_stage_position,
    find_subject_inputs,
)
from app.runtime.lineage import RowParent


def find_contributing_rows(run_branches: WorkflowRunBranches,
                           stage_id: StageId, row_ordinal: RowOrdinal) -> RowSet:
    """Replace a merged row by the rows merged into it, past every merge on the route."""
    reached, at_stage, through = _expand(
        run_branches, stage_id, row_ordinal,
        _find_the_stage_the_earliest_merge_read(run_branches, (stage_id, row_ordinal)))
    ordinals = sorted(reached)
    return RowSet(at_stage=at_stage, ordinals=ordinals, regrained_at=through,
                  fed_by_no_rows=[ordinal for ordinal in ordinals
                                  if _fed_by_no_rows(run_branches, at_stage, ordinal)])


def find_sample_choices_behind(run_branches: WorkflowRunBranches, stage_id: StageId,
                               row_ordinal: RowOrdinal
                               ) -> dict[RowOrdinal, tuple[RowRef, ...]]:
    """Per row behind the cited one, the row to sample at each fan-in between."""
    reached, _, _ = _expand(
        run_branches, stage_id, row_ordinal,
        _find_the_stage_the_earliest_merge_read(run_branches, (stage_id, row_ordinal)))
    return reached


def find_stages_on_route(run_branches: WorkflowRunBranches,
                         rows: Sequence[RowRef]) -> set[StageId]:
    """Every stage these rows came through. A branch anywhere else tells them nothing."""
    return set(find_rows_reached_per_stage(run_branches, rows))


def find_rows_reached_per_stage(run_branches: WorkflowRunBranches,
                                rows: Sequence[RowRef]
                                ) -> dict[StageId, set[RowOrdinal]]:
    """Which rows of each stage on the route these rows came through."""
    return _reach_upstream(run_branches, rows)


def find_nearest_merge(run_branches: WorkflowRunBranches,
                       rows: Sequence[RowRef]) -> StageId | None:
    """The re-graining closest to the cited cell — the only one a drawing resolves."""
    on_route = find_stages_on_route(run_branches, rows)
    merged = find_merge_stage_ids(run_branches)
    crossed = [sid for sid in run_branches.ordered_stage_ids
               if sid in on_route and sid in merged]
    return crossed[-1] if crossed else None


def find_merge_stage_ids(run_branches: WorkflowRunBranches) -> set[StageId]:
    """A stage whose lineage says several input rows became one output row."""
    return {option.stage_id for option in run_branches.branch_options.values()
            if option.reason is BranchReason.merge}


def measure_frame_scale(run_branches: WorkflowRunBranches,
                        citation: StageOutputCellCitation) -> list[FrameScale]:
    """Per stage: rows in the frame, and how many of them reached the figure."""
    reached = _reach_upstream(run_branches, [(citation.stage_id, citation.row_ordinal)])
    return [FrameScale(stage=sid, rows_count=run_branches.row_counts[sid],
                       included_rows_count=len(reached[sid]))
            for sid in run_branches.ordered_stage_ids
            if sid in reached and run_branches.row_counts[sid]]


def find_stages_beside_the_flow(run_branches: WorkflowRunBranches,
                                from_stage: StageId) -> set[StageId]:
    """Stages no path reaches from `from_stage` without crossing into a lookup table."""
    return set(run_branches.stages) - _walk_back_along_the_flow(run_branches, from_stage)


def find_stages_each_row_came_through(
        run_branches: WorkflowRunBranches, at_stage: StageId,
        ordinals: list[RowOrdinal]) -> list[list[StageId]]:
    """Per row: every frame it was a row of, which its branch path holds only part of."""
    memo: dict[tuple[StageId, RowOrdinal], frozenset[StageId]] = {}
    # Rows of `at_stage` go on to be rows of everything the flow carries them into.
    below = _walk_on_along_the_flow(run_branches, at_stage)
    return [sorted(_came_through(run_branches, at_stage, ordinal, memo) | below)
            for ordinal in ordinals]


def find_stages_each_stage_feeds(run_branches: WorkflowRunBranches
                                 ) -> dict[StageId, set[StageId]]:
    """Transitive, so an input can be matched to a stage well below the one reading it."""
    children: dict[StageId, set[StageId]] = {}
    for stage_id, stage in run_branches.stages.items():
        for read in stage.inputs:
            children.setdefault(read.id, set()).add(stage_id)
    return {stage_id: _walk_down(children, stage_id)
            for stage_id in run_branches.stages}


def _walk_down(children: dict[StageId, set[StageId]],
               stage_id: StageId) -> set[StageId]:
    below: set[StageId] = set()
    frontier = list(children.get(stage_id, ()))
    while frontier:
        below.add(next_id := frontier.pop())
        frontier.extend(child for child in children.get(next_id, ())
                        if child not in below)
    return below


def _expand(run_branches: WorkflowRunBranches, stage_id: StageId, ordinal: RowOrdinal,
            stop_at_stage: StageId | None
            ) -> tuple[dict[RowOrdinal, tuple[RowRef, ...]], StageId, list[StageId]]:
    """Each reached row against the rows a walk down to it has to sample."""
    if stage_id == stop_at_stage:
        return {ordinal: ()}, stage_id, []
    merged_from = _merged_from(run_branches, stage_id, ordinal)
    if merged_from is None:
        above = (None if stop_at_stage is None else _read_the_only_upstream_row(
            run_branches, (stage_id, ordinal), stop_at_stage))
        if above is None:
            return {ordinal: ()}, stage_id, []
        return _expand(run_branches, above[0], above[1], stop_at_stage)
    # A merge names one input stage, whose rows were all merged or none were.
    from_each_parent = [(parent, _expand(run_branches, parent.stage_id,
                                         parent.row_ordinal, stop_at_stage))
                        for parent in merged_from]
    _, (_, at_stage, below) = from_each_parent[0]
    gathered: dict[RowOrdinal, tuple[RowRef, ...]] = {}
    for parent, (reached, _, _) in from_each_parent:
        step = (parent.stage_id, parent.row_ordinal)
        for row, choices in reached.items():
            gathered.setdefault(row, (step, *choices))
    return gathered, at_stage, [stage_id] + below


def _find_the_stage_the_earliest_merge_read(run_branches: WorkflowRunBranches,
                                            cited: RowRef) -> StageId | None:
    """Where a walk back from `cited` stops. docs/branch-analysis.md"""
    on_route = set(_reach_upstream(run_branches, [cited]))
    merged = find_merge_stage_ids(run_branches) & on_route
    earliest = next((sid for sid in run_branches.ordered_stage_ids if sid in merged), None)
    if earliest is None:
        return None
    lineage = run_branches.lineages.get(earliest)
    return next((parent.stage_id for parents in (lineage.parents if lineage else [])
                 for parent in parents if parent.kind == MERGE_EDGE), None)


def _read_the_only_upstream_row(run_branches: WorkflowRunBranches, row: RowRef,
                                stop_at_stage: StageId) -> RowRef | None:
    """The one row `row` was made from, on the input it is a version of — never a lookup."""
    stage_id, ordinal = row
    order = run_branches.ordered_stage_ids
    if find_stage_position(order, stage_id) <= find_stage_position(order, stop_at_stage):
        return None
    stage = run_branches.stages.get(stage_id)
    subject = stage.inputs[0].id if stage and stage.inputs else None
    if subject is None:
        return None
    lineage = run_branches.lineages.get(stage_id)
    if lineage is None or ordinal >= len(lineage.parents):
        return (subject, ordinal)
    on_subject = [p for p in lineage.parents[ordinal] if p.stage_id == subject]
    return (subject, on_subject[0].row_ordinal) if len(on_subject) == 1 else None


def _fed_by_no_rows(run_branches: WorkflowRunBranches, stage_id: StageId,
                    ordinal: RowOrdinal) -> bool:
    """The row is in the frame and the lineage names nothing that produced it."""
    lineage = run_branches.lineages.get(stage_id)
    return (lineage is not None and ordinal < len(lineage.parents)
            and not lineage.parents[ordinal])


def _merged_from(run_branches: WorkflowRunBranches, stage_id: StageId,
                 ordinal: RowOrdinal) -> list[RowParent] | None:
    """None when the row is its own row set; a list when several rows made it."""
    lineage = run_branches.lineages.get(stage_id)
    if lineage is None or ordinal >= len(lineage.parents):
        return None
    merged = [p for p in lineage.parents[ordinal] if p.kind == MERGE_EDGE]
    return merged or None


def _reach_upstream(run_branches: WorkflowRunBranches, rows: Sequence[RowRef]
                    ) -> dict[StageId, set[RowOrdinal]]:
    """Every (stage, row) the named rows came from, ignoring what each hop meant."""
    found: dict[StageId, set[RowOrdinal]] = {}
    front = list(rows)
    seen = set(front)
    while front:
        sid, row = front.pop()
        found.setdefault(sid, set()).add(row)
        for step in _one_hop_up(run_branches, sid, row):
            if step not in seen:
                seen.add(step)
                front.append(step)
    return found


def _walk_on_along_the_flow(run_branches: WorkflowRunBranches,
                            from_stage: StageId) -> frozenset[StageId]:
    carried: set[StageId] = set()
    front = [from_stage]
    while front:
        sid = front.pop()
        for below, stage in run_branches.stages.items():
            if sid in find_subject_inputs(stage) and below not in carried:
                carried.add(below)
                front.append(below)
    return frozenset(carried)


def _walk_back_along_the_flow(run_branches: WorkflowRunBranches,
                              from_stage: StageId) -> set[StageId]:
    on_flow: set[StageId] = set()
    front = [from_stage]
    while front:
        sid = front.pop()
        if sid in on_flow or sid not in run_branches.stages:
            continue
        on_flow.add(sid)
        front.extend(find_subject_inputs(run_branches.stages[sid]))
    return on_flow


def _came_through(run_branches: WorkflowRunBranches, sid: StageId, row: RowOrdinal,
                  memo: dict[tuple[StageId, RowOrdinal], frozenset[StageId]]
                  ) -> frozenset[StageId]:
    key = (sid, row)
    if key not in memo:
        found = {sid}
        # A load names its own rows as their parents, to hold the file they came out of.
        for parent in _one_hop_up(run_branches, sid, row):
            if parent != key:
                found |= _came_through(run_branches, parent[0], parent[1], memo)
        memo[key] = frozenset(found)
    return memo[key]


def _one_hop_up(run_branches: WorkflowRunBranches, sid: StageId, row: RowOrdinal
                ) -> list[tuple[StageId, RowOrdinal]]:
    lineage = run_branches.lineages.get(sid)
    if lineage is not None and row < len(lineage.parents):
        return [(p.stage_id, p.row_ordinal) for p in lineage.parents[row]]
    # No lineage: the stage type's contract says output row i IS input row i.
    stage = run_branches.stages.get(sid)
    inputs = [ref.id for ref in stage.inputs] if stage else []
    return [(inputs[0], row)] if len(inputs) == 1 else []
