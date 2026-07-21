"""Resolve how an eval run can be scored: walk the executed frontier from the
target and decide whether it is row-alignable end to end.

This is eval-gate logic (it returns an EvalRunSettings and reasons about the
scoring path), so it lives in app.evals rather than in the core eval models. It
reads the grain-and-order fact from core (app.core.models) like every other layer."""
from __future__ import annotations

from typing import Iterable

from app.core.models import EvalRunSettings, Workflow


def resolve_eval_run_settings(
    workflow: Workflow,
    overrides: Iterable[str],
    target: str,
) -> EvalRunSettings:
    """Walk the executed frontier from `target` upward, stopping at overrides, and
    decide whether it can be scored automatically row-by-row (every frontier stage
    grain-preserving) — the v1 condition for a single-table, row-aligned eval.

    Whether an eval needs a code scorer is a property of the *path*, not the
    author's preference — this function is where that's decided. It raises
    (loudly) if `target` or any override names no stage, or if `target` is itself
    overridden — a misconfigured eval should fail at definition, not at score time.
    """
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
