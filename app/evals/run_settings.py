"""Resolve how an eval run can be scored: walk the executed frontier from the
target and decide whether it is row-alignable end to end.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

from app.models import EvalRunSettings, Workflow
from app.models.workflow import StageInGraph


def resolve_eval_run_settings(
    workflow: Workflow,
    dataset_stage: Optional[str],
    reference_overrides: Iterable[str],
    target: str,
) -> EvalRunSettings:
    by_id = workflow.index_stages_by_id()
    injected = _validate_stages_named(by_id, dataset_stage, reference_overrides, target)
    frontier = _list_executed_frontier(by_id, injected, target)
    blocking = sorted(
        {stage_id for stage_id in frontier
         if not by_id[stage_id].is_grain_and_order_preserving}
        | _find_stages_reading_the_dataset_off_their_spine(
            by_id, frontier, injected, dataset_stage)
    )
    return EvalRunSettings(can_score_declaratively=not blocking,
                           frontier=sorted(frontier), blocking_stages=blocking)


def _validate_stages_named(
    by_id: Mapping[str, StageInGraph],
    dataset_stage: Optional[str],
    reference_overrides: Iterable[str],
    target: str,
) -> set[str]:
    if target not in by_id:
        raise ValueError(f"target {target!r} is not a stage in the workflow")
    injected = set(reference_overrides) | ({dataset_stage} if dataset_stage else set())
    missing = injected - by_id.keys()
    if missing:
        raise ValueError(f"override(s) reference no stage: {sorted(missing)}")
    if target in injected:
        raise ValueError(f"target {target!r} cannot also be an override")
    return injected


def _list_executed_frontier(
    by_id: Mapping[str, StageInGraph], injected: set[str], target: str
) -> list[str]:
    frontier: list[str] = []
    seen: set[str] = set()
    stack = [target]
    while stack:
        node = stack.pop()
        if node in seen or node in injected:
            continue  # an overridden node is injected, not executed — and we
            # don't traverse above it; its upstream doesn't run either.
        seen.add(node)
        frontier.append(node)
        for upstream in by_id[node].input_ids:
            if upstream not in seen and upstream not in injected:
                stack.append(upstream)
    return frontier


def _find_stages_reading_the_dataset_off_their_spine(
    by_id: Mapping[str, StageInGraph],
    frontier: Iterable[str],
    injected: set[str],
    dataset_stage: Optional[str],
) -> set[str]:
    """A preserving multi-input stage aligns along inputs[0]; the dataset elsewhere misaligns."""
    if dataset_stage is None:
        return set()
    return {
        stage_id for stage_id in frontier
        if any(_reads_the_dataset(by_id, upstream, injected, dataset_stage)
               for upstream in by_id[stage_id].input_ids[1:])
    }


def _reads_the_dataset(
    by_id: Mapping[str, StageInGraph],
    node: str,
    injected: set[str],
    dataset_stage: str,
) -> bool:
    if node == dataset_stage:
        return True
    if node in injected or node not in by_id:
        return False
    return any(_reads_the_dataset(by_id, upstream, injected, dataset_stage)
               for upstream in by_id[node].input_ids)
