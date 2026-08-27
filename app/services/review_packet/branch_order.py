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
    return _walk_one_branch_at_a_time({stage.id: stage for stage in stages})


def _walk_one_branch_at_a_time(stages_by_id: dict[ID, _StageT]) -> list[_StageT]:
    parents_by_id = _collect_parents_by_id(stages_by_id)
    children_by_id = _collect_children_by_id(parents_by_id)
    waiting_by_id = {stage_id: len(up) for stage_id, up in parents_by_id.items()}
    root_ids = [stage_id for stage_id, up in parents_by_id.items() if not up]
    stalled_ids: list[ID] = []
    walked_ids: list[ID] = []
    pending_ids: list[ID] = []
    while root_ids or pending_ids:
        if not pending_ids:
            pending_ids.append(
                _take_next_root_id(root_ids, stalled_ids, waiting_by_id, children_by_id)
            )
        stage_id = pending_ids.pop()
        walked_ids.append(stage_id)
        freed = _free_child_ids(stage_id, children_by_id, waiting_by_id, stalled_ids)
        pending_ids.extend(reversed(freed))
    _refuse_unwalked(parents_by_id, walked_ids)
    return [stages_by_id[stage_id] for stage_id in walked_ids]


def _take_next_root_id(
    root_ids: list[ID],
    stalled_ids: Sequence[ID],
    waiting_by_id: dict[ID, int],
    children_by_id: dict[ID, list[ID]],
) -> ID:
    """The branch to pick up next is the one the earliest stalled join is short of."""
    target_id = next((s for s in stalled_ids if waiting_by_id[s]), None)
    taken_id = next(
        (r for r in root_ids if target_id is not None and _reaches(r, target_id, children_by_id)),
        root_ids[0],
    )
    root_ids.remove(taken_id)
    return taken_id


def _free_child_ids(
    stage_id: ID,
    children_by_id: dict[ID, list[ID]],
    waiting_by_id: dict[ID, int],
    stalled_ids: list[ID],
) -> list[ID]:
    freed_ids: list[ID] = []
    for child_id in children_by_id[stage_id]:
        waiting_by_id[child_id] -= 1
        if waiting_by_id[child_id] == 0:
            freed_ids.append(child_id)
        elif child_id not in stalled_ids:
            stalled_ids.append(child_id)
    return freed_ids


def _reaches(start_id: ID, target_id: ID, children_by_id: dict[ID, list[ID]]) -> bool:
    seen_ids: set[ID] = set()
    pending_ids = [start_id]
    while pending_ids:
        stage_id = pending_ids.pop()
        if stage_id == target_id:
            return True
        pending_ids.extend(c for c in children_by_id[stage_id] if c not in seen_ids)
        seen_ids.update(children_by_id[stage_id])
    return False


def _collect_parents_by_id(stages_by_id: dict[ID, _StageT]) -> dict[ID, list[ID]]:
    """An input naming no stage here is dropped, so a join is not left waiting on it."""
    return {
        stage_id: [up for up in stage.input_ids if up in stages_by_id]
        for stage_id, stage in stages_by_id.items()
    }


def _collect_children_by_id(parents_by_id: dict[ID, list[ID]]) -> dict[ID, list[ID]]:
    children_by_id: dict[ID, list[ID]] = {stage_id: [] for stage_id in parents_by_id}
    for stage_id, parent_ids in parents_by_id.items():
        for parent_id in parent_ids:
            children_by_id[parent_id].append(stage_id)
    return children_by_id


def _refuse_unwalked(parents_by_id: dict[ID, list[ID]], walked_ids: Sequence[ID]) -> None:
    unwalked_ids = sorted(set(parents_by_id) - set(walked_ids))
    if unwalked_ids:
        raise ValueError(f"cyclic stages, cannot order: {unwalked_ids}")
