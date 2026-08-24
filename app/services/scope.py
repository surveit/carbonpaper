"""Which rows produced a cited cell, over a run's branches. docs/branch-analysis.md."""

from __future__ import annotations

from app.core.errors import UnresolvableFigure
from app.models.branch_analysis import FrameScale, RowOrdinal, RowSet
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.runtime.branch_analysis.run_branches import (
    CONTRIBUTION,
    WorkflowRunBranches,
    find_reference_inputs,
)


def find_contributing_rows(run: WorkflowRunBranches, citation: StageOutputCellCitation
                           ) -> RowSet:
    """Replace a group row by the rows that fed it, until none is a group."""
    reached, at_stage, through = _expand(run, citation.stage_id, citation.row_ordinal)
    return RowSet(at_stage=at_stage, ordinals=sorted(set(reached)),
                  aggregates_walked_down=through)


def measure_frame_scale(run: WorkflowRunBranches, citation: StageOutputCellCitation
                        ) -> list[FrameScale]:
    """Per stage: rows in the frame, and how many of them reached the figure."""
    reached = _reach_upstream(run, citation.stage_id, citation.row_ordinal)
    lookups = find_reference_inputs(run.stages)
    return [FrameScale(stage=sid, rows_count=run.rows[sid],
                       included_rows_count=len(reached[sid]),
                       is_a_lookup_table=sid in lookups)
            for sid in run.order if sid in reached and run.rows[sid]]


def _expand(run: WorkflowRunBranches, stage_id: StageId, ordinal: RowOrdinal
            ) -> tuple[list[RowOrdinal], StageId, list[StageId]]:
    fed_by = _contributors(run, stage_id, ordinal)
    if fed_by is None:
        return [ordinal], stage_id, []
    if not fed_by:
        return [], stage_id, []
    gathered: list[RowOrdinal] = []
    landed: set[StageId] = set()
    below: list[StageId] = []
    for parent in fed_by:
        rows, at_stage, deeper = _expand(run, parent.stage_id, parent.row_ordinal)
        gathered.extend(rows)
        landed.add(at_stage)
        below = deeper
    if len(landed) > 1:
        raise UnresolvableFigure(
            f"{stage_id} row {ordinal} bottoms out in {sorted(landed)}; "
            f"a set of contributing rows must sit at one grain")
    return gathered, landed.pop(), [stage_id] + below


def _contributors(run: WorkflowRunBranches, stage_id: StageId, ordinal: RowOrdinal):
    """None when the row is its own row set; a list when it is a group."""
    lineage = run.lineages.get(stage_id)
    if lineage is None or ordinal >= len(lineage.parents):
        return None
    fed_by = [p for p in lineage.parents[ordinal] if p.kind == CONTRIBUTION]
    return fed_by or None


def _reach_upstream(run: WorkflowRunBranches, stage_id: StageId, ordinal: RowOrdinal
                    ) -> dict[StageId, set[RowOrdinal]]:
    """Every (stage, row) the cited row came from, ignoring what each hop meant."""
    found: dict[StageId, set[RowOrdinal]] = {}
    front = [(stage_id, ordinal)]
    seen = {(stage_id, ordinal)}
    while front:
        sid, row = front.pop()
        found.setdefault(sid, set()).add(row)
        for step in _one_hop_up(run, sid, row):
            if step not in seen:
                seen.add(step)
                front.append(step)
    return found


def _one_hop_up(run: WorkflowRunBranches, sid: StageId, row: RowOrdinal
                ) -> list[tuple[StageId, RowOrdinal]]:
    lineage = run.lineages.get(sid)
    if lineage is not None and row < len(lineage.parents):
        return [(p.stage_id, p.row_ordinal) for p in lineage.parents[row]]
    stage = run.stages.get(sid)
    inputs = [ref.id for ref in stage.inputs] if stage else []
    if len(inputs) == 1 and run.rows[inputs[0]] == run.rows[sid]:
        return [(inputs[0], row)]  # row-preserving: output i is input i
    return []
