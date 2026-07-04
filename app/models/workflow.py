"""Workflow contract: a workflow is a list of validated stages plus the
cross-stage checks (unique ids, inputs resolve, acyclic).

The graph checks are plain functions so they can be tested on their own and read
without wading through a validator. Each returns a list of human-readable issue
strings ([] means it found nothing) — the whole batch is collected so one call
surfaces every problem, not just the first.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError, model_validator

from app.models.schema import _Base, format_errors
from app.models.stage import Stage


def check_unique_ids(stages: list[Stage]) -> list[str]:
    """One issue per stage id that appears more than once."""
    ids = [s.id for s in stages]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    return [f"duplicate stage id `{d}`" for d in dupes]


def check_inputs_resolve(stages: list[Stage]) -> list[str]:
    """One issue per input that names no existing stage — all of them, so a
    reviewer fixes every dangling edge in one pass rather than one per re-run."""
    ids = {s.id for s in stages}
    issues: list[str] = []
    for s in stages:
        for upstream in s.input_ids:
            if upstream not in ids:
                issues.append(f"`{s.id}`: input `{upstream}` references no stage")
    return issues


def detect_cycle(stages: list[Stage]) -> list[str]:
    """A one-item list naming the first cycle found, or [] if acyclic. One cycle
    is enough to reject the workflow; we don't enumerate them all. The stage graph
    must stay acyclic — a cycle means the runner could never order the stages."""
    edges = {s.id: list(s.input_ids) for s in stages}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in edges}
    found: list[str] = []

    def visit(node: str, path: list[str]) -> None:
        if found:
            return
        color[node] = GRAY
        for nxt in edges.get(node, []):
            if found:
                return
            if color.get(nxt) == GRAY:
                found.append(f"cycle detected: {' -> '.join(path + [node, nxt])}")
                return
            if color.get(nxt) == WHITE:
                visit(nxt, path + [node])
        color[node] = BLACK

    for sid in edges:
        if color[sid] == WHITE:
            visit(sid, [])
    return found


def graph_issues(stages: list[Stage]) -> list[str]:
    """Every cross-stage problem in the workflow graph: duplicate ids, dangling
    inputs, and a cycle. The single source of truth both the strict model
    validator and the non-fatal `validate_workflow` build on."""
    return check_unique_ids(stages) + check_inputs_resolve(stages) + detect_cycle(stages)


class Workflow(_Base):
    """A whole workflow: validated stages with unique ids, resolvable inputs, acyclic."""
    stages: list[Stage]

    @model_validator(mode="after")
    def _validate_graph(self) -> "Workflow":
        issues = graph_issues(self.stages)
        if issues:
            raise ValueError("; ".join(issues))
        return self


def parse_workflow(stages: list[dict[str, Any]]) -> Workflow:
    """Parse + validate a list of stage dicts. Raises ValidationError if invalid."""
    return Workflow.model_validate({"stages": list(stages)})


def validate_workflow(stages: list[Stage]) -> list[str]:
    """Cross-stage checks (unique ids, inputs resolve, acyclic) on
    already-validated stages, as human-readable issue strings — every problem,
    not just the first."""
    return graph_issues(stages)


def validate_workflow_draft(stages: list[dict[str, Any]]) -> list[str]:
    """Non-fatal validation of DRAFT stage dicts (e.g. a compiler's LLM output):
    parse + validate the whole list and return human-readable issues ([] means a
    clean-validating draft). Unlike validate_workflow, which runs the graph checks
    on already-parsed Stages, this also surfaces per-stage schema errors straight
    from raw dicts, so a caller can show problems instead of crashing."""
    try:
        Workflow.model_validate({"stages": list(stages)})
        return []
    except ValidationError as err:
        return format_errors(err)
