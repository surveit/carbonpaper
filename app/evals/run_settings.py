"""Resolve how an eval run can be scored: walk the executed frontier from the
target and decide whether it is row-alignable end to end.
"""
from __future__ import annotations

from typing import Iterable

from app.models import EvalRunSettings, Workflow


def resolve_eval_run_settings(
    workflow: Workflow,
    overrides: Iterable[str],
    target: str,
) -> EvalRunSettings:
    by_id = workflow.index_stages_by_id()
    if target not in by_id:
        raise ValueError(f"target {target!r} is not a stage in the workflow")
    ov = set(overrides)
    missing = ov - by_id.keys()
    if missing:
        raise ValueError(f"override(s) reference no stage: {sorted(missing)}")
    if target in ov:
        raise ValueError(f"target {target!r} cannot also be an override")

    frontier: list[str] = []
    seen: set[str] = set()
    stack = [target]
    while stack:
        node = stack.pop()
        if node in seen or node in ov:
            continue  # an overridden node is injected, not executed — and we
            # don't traverse above it; its upstream doesn't run either.
        seen.add(node)
        frontier.append(node)
        for upstream in by_id[node].input_ids:
            if upstream not in seen and upstream not in ov:
                stack.append(upstream)

    blocking = sorted(n for n in frontier if not by_id[n].is_grain_and_order_preserving)
    return EvalRunSettings(can_score_declaratively=not blocking,
                           frontier=sorted(frontier), blocking_stages=blocking)
