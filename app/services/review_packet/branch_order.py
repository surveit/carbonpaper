"""Reading order over a workflow graph: one branch carried to the step that joins it
to another, then the branch that step was waiting on, then the join itself."""
from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from app.core.ids import ID
from app.models.workflow import StageInGraph

_StageT = TypeVar("_StageT", bound=StageInGraph)


def sort_stages_by_branch(stages: Sequence[_StageT]) -> list[_StageT]:
    """Ties break on the given order — the first root, and the first freed child, lead."""
    by_id = {stage.id: stage for stage in stages}
    parents = {
        stage_id: [upstream for upstream in stage.input_ids if upstream in by_id]
        for stage_id, stage in by_id.items()
    }
    return [by_id[stage_id] for stage_id in _walk_one_branch_at_a_time(parents)]


def _walk_one_branch_at_a_time(parents: dict[ID, list[ID]]) -> list[ID]:
    children = _collect_children(parents)
    waiting = {stage_id: len(upstream) for stage_id, upstream in parents.items()}
    roots = [stage_id for stage_id, upstream in parents.items() if not upstream]
    stalled: list[ID] = []
    walked: list[ID] = []
    stack: list[ID] = []
    while roots or stack:
        if not stack:
            stack.append(_take_root_the_stall_waits_on(roots, stalled, waiting, children))
        stage_id = stack.pop()
        walked.append(stage_id)
        stack.extend(reversed(_free_children(stage_id, children, waiting, stalled)))
    _refuse_unwalked(parents, walked)
    return walked


def _take_root_the_stall_waits_on(
    roots: list[ID],
    stalled: Sequence[ID],
    waiting: dict[ID, int],
    children: dict[ID, list[ID]],
) -> ID:
    # A join is emitted only once its last parent has been, so the branch to pick up
    # next is whichever one the join stopped at is still short of.
    target = next((stage_id for stage_id in stalled if waiting[stage_id]), None)
    taken = next(
        (root for root in roots if target is not None and _reaches(root, target, children)),
        roots[0],
    )
    roots.remove(taken)
    return taken


def _free_children(
    stage_id: ID,
    children: dict[ID, list[ID]],
    waiting: dict[ID, int],
    stalled: list[ID],
) -> list[ID]:
    freed: list[ID] = []
    for child in children[stage_id]:
        waiting[child] -= 1
        if waiting[child] == 0:
            freed.append(child)
        elif child not in stalled:
            stalled.append(child)
    return freed


def _reaches(start: ID, target: ID, children: dict[ID, list[ID]]) -> bool:
    seen: set[ID] = set()
    stack = [start]
    while stack:
        stage_id = stack.pop()
        if stage_id == target:
            return True
        stack.extend(child for child in children[stage_id] if child not in seen)
        seen.update(children[stage_id])
    return False


def _collect_children(parents: dict[ID, list[ID]]) -> dict[ID, list[ID]]:
    children: dict[ID, list[ID]] = {stage_id: [] for stage_id in parents}
    for stage_id, upstream in parents.items():
        for parent in upstream:
            children[parent].append(stage_id)
    return children


def _refuse_unwalked(parents: dict[ID, list[ID]], walked: Sequence[ID]) -> None:
    unwalked = sorted(set(parents) - set(walked))
    if unwalked:
        raise ValueError(f"cyclic stages, cannot order: {unwalked}")
