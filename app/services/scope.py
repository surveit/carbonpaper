"""Which rows produced a cited cell, over a run's branches. docs/branch-analysis.md."""

from __future__ import annotations

from app.models.branch_analysis import (
    BranchId,
    BranchPath,
    BranchReason,
    FrameScale,
    PathBehindFigure,
    PathsBehindFigure,
    RowOrdinal,
    RowSet,
)
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.runtime.branch_analysis.run_branches import (
    MERGE_EDGE,
    WorkflowRunBranches,
    find_reference_inputs,
)
from app.runtime.errors import MissingLineage
from app.runtime.lineage import RowParent


def find_contributing_rows(run_branches: WorkflowRunBranches,
                           stage_id: StageId, row_ordinal: RowOrdinal) -> RowSet:
    """Replace a merged row by the rows merged into it, until none was merged."""
    reached, at_stage, through = _expand(run_branches, stage_id, row_ordinal)
    ordinals = sorted(set(reached))
    return RowSet(at_stage=at_stage, ordinals=ordinals, regrained_at=through,
                  fed_by_no_rows=[ordinal for ordinal in ordinals
                                  if _fed_by_no_rows(run_branches, at_stage, ordinal)])


def find_merges_that_excluded(run_branches: WorkflowRunBranches,
                              citation: StageOutputCellCitation) -> set[BranchId]:
    """A merge branch excludes rows only relative to a citation, so it is asked here."""
    return {branch_id
            for branch_id, option in run_branches.branch_options.items()
            if option.merged_into_row_ordinal is not None
            and option.stage_id == citation.stage_id
            and option.merged_into_row_ordinal != citation.row_ordinal}


def measure_frame_scale(run_branches: WorkflowRunBranches,
                        citation: StageOutputCellCitation) -> list[FrameScale]:
    """Per stage: rows in the frame, and how many of them reached the figure."""
    reached = _reach_upstream(run_branches, citation.stage_id, citation.row_ordinal)
    return [FrameScale(stage=sid, rows_count=run_branches.row_counts[sid],
                       included_rows_count=len(reached[sid]))
            for sid in run_branches.ordered_stage_ids
            if sid in reached and run_branches.row_counts[sid]]


def find_lookup_table_stages(run_branches: WorkflowRunBranches) -> set[StageId]:
    """A lookup table's size is not the flow narrowing, so a funnel leaves it out."""
    return find_reference_inputs(run_branches.stages)


def _expand(run_branches: WorkflowRunBranches, stage_id: StageId, ordinal: RowOrdinal
            ) -> tuple[list[RowOrdinal], StageId, list[StageId]]:
    merged_from = _merged_from(run_branches, stage_id, ordinal)
    if merged_from is None:
        return [ordinal], stage_id, []
    # A merge names one input stage, whose rows were all merged or none were.
    from_each_parent = [_expand(run_branches, parent.stage_id, parent.row_ordinal)
                        for parent in merged_from]
    _, at_stage, below = from_each_parent[0]
    gathered = [row for rows, _, _ in from_each_parent for row in rows]
    return gathered, at_stage, [stage_id] + below


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


def _reach_upstream(run_branches: WorkflowRunBranches, stage_id: StageId,
                    ordinal: RowOrdinal) -> dict[StageId, set[RowOrdinal]]:
    """Every (stage, row) the cited row came from, ignoring what each hop meant."""
    found: dict[StageId, set[RowOrdinal]] = {}
    front = [(stage_id, ordinal)]
    seen = {(stage_id, ordinal)}
    while front:
        sid, row = front.pop()
        found.setdefault(sid, set()).add(row)
        for step in _one_hop_up(run_branches, sid, row):
            if step not in seen:
                seen.add(step)
                front.append(step)
    return found


def _one_hop_up(run_branches: WorkflowRunBranches, sid: StageId, row: RowOrdinal
                ) -> list[tuple[StageId, RowOrdinal]]:
    lineage = run_branches.lineages.get(sid)
    if lineage is not None and row < len(lineage.parents):
        return [(p.stage_id, p.row_ordinal) for p in lineage.parents[row]]
    # No lineage: the stage type's contract says output row i IS input row i.
    stage = run_branches.stages.get(sid)
    inputs = [ref.id for ref in stage.inputs] if stage else []
    return [(inputs[0], row)] if len(inputs) == 1 else []


def find_paths_behind(run_branches: WorkflowRunBranches, at_stage: StageId,
                      ordinals: list[RowOrdinal], on_route: set[StageId],
                      marked_row: RowOrdinal | None = None) -> PathsBehindFigure:
    """Every distinct route the rows took, each with one row that took it."""
    _refuse_a_frame_with_no_paths(run_branches, at_stage, ordinals)
    paths, index = index_paths(run_branches, at_stage, ordinals, on_route)
    took = _gather_ordinals_per_path(ordinals, index, len(paths))
    shared = _find_branches_on_every_path(paths)
    return PathsBehindFigure(
        at_stage=at_stage,
        paths=[_read_one_path(run_branches, path, on_it, shared, marked_row)
               for path, on_it in zip(paths, took)],
    )


def index_paths(run_branches: WorkflowRunBranches, at_stage: StageId,
                ordinals: list[RowOrdinal], on_route: set[StageId]
                ) -> tuple[list[BranchPath], list[int]]:
    """Distinct paths, and one small int per row."""
    paths: list[BranchPath] = []
    seen: dict[BranchPath, int] = {}
    index = []
    for ordinal in ordinals:
        path = _keep_branches_on_route(
            run_branches, run_branches.branch_paths[at_stage][ordinal], on_route)
        if path not in seen:
            seen[path] = len(paths)
            paths.append(path)
        index.append(seen[path])
    return paths, index


def _read_one_path(run_branches: WorkflowRunBranches, path: BranchPath,
                   ordinals: list[RowOrdinal], shared: frozenset[BranchId],
                   marked_row: RowOrdinal | None) -> PathBehindFigure:
    options = [run_branches.branch_options[branch_id] for branch_id in path]
    return PathBehindFigure(
        rows=len(ordinals),
        tells_it_apart=[o for o in options if o.id not in shared],
        whole_path=options,
        example_ordinal=ordinals[0],
        holds_the_marked_row=marked_row in ordinals,
    )


def _refuse_a_frame_with_no_paths(run_branches: WorkflowRunBranches, at_stage: StageId,
                                  ordinals: list[RowOrdinal]) -> None:
    """A stage the reconstruction never sized holds no path for any of its rows."""
    held = len(run_branches.branch_paths.get(at_stage) or [])
    if ordinals and held <= max(ordinals):
        raise MissingLineage(
            f"this run recorded paths for {held} rows of {at_stage}, "
            f"not the {max(ordinals) + 1} the figure reaches"
        )


def _keep_branches_on_route(run_branches: WorkflowRunBranches, path: BranchPath,
                            on_route: set[StageId]) -> BranchPath:
    """A merge into a row this figure is not tells these rows nothing, so it is dropped."""
    options = run_branches.branch_options
    return tuple(branch_id for branch_id in path
                 if options[branch_id].reason is not BranchReason.merge
                 or options[branch_id].stage_id in on_route)


def _gather_ordinals_per_path(ordinals: list[RowOrdinal], index: list[int], paths: int
                              ) -> list[list[RowOrdinal]]:
    took: list[list[RowOrdinal]] = [[] for _ in range(paths)]
    for at, which in enumerate(index):
        took[which].append(ordinals[at])
    return took


def _find_branches_on_every_path(paths: list[BranchPath]) -> frozenset[BranchId]:
    """One path tells itself apart from nothing, so nothing of it reads as shared."""
    if len(paths) < 2:
        return frozenset()
    return frozenset.intersection(*(frozenset(path) for path in paths))
