"""Workflow contract: validated stages plus the cross-stage checks (unique ids, inputs
resolve, acyclic). A check answerable from one stage alone belongs on `Stage`, not here.

Each check returns a list of issue strings ([] means it found nothing), and the whole
batch is collected so one call surfaces every problem, not just the first.
"""
from __future__ import annotations

from typing import Any, Sequence, TypeVar

from pydantic import ValidationError, model_validator

from app.models.schema import _Base
from app.models.stage import Stage, AuthoredStageFields, StageType
from app.core.utils import format_errors

# Ordering and cycle detection read only the shared fields, so they hold a
# submitted draft and a stored stage alike, and hand back what they were given.
_StageT = TypeVar("_StageT", bound=AuthoredStageFields)


def validate_unique_ids(stages: Sequence[AuthoredStageFields]) -> list[str]:
    ids = [s.id for s in stages]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    return [f"duplicate stage id `{d}`" for d in dupes]


def validate_inputs_resolve(stages: list[Stage]) -> list[str]:
    ids = {s.id for s in stages}
    issues: list[str] = []
    for s in stages:
        for upstream in s.input_ids:
            if upstream not in ids:
                issues.append(f"`{s.id}`: input `{upstream}` references no stage")
    return issues


def detect_cycle(stages: Sequence[AuthoredStageFields]) -> list[str]:
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


def sort_stages_by_dependency(stages: Sequence[_StageT]) -> list[_StageT]:
    """Assumes ids are unique (`validate_unique_ids`); a duplicate silently misorders."""
    pending = list(stages)
    ordered: list[_StageT] = []
    while pending:
        waiting = {s.id for s in pending}
        ready = [s for s in pending if not waiting & set(s.input_ids)]
        if not ready:
            raise ValueError(f"cyclic stages, cannot order: {sorted(waiting)}")
        ordered.extend(ready)
        ready_ids = {s.id for s in ready}
        pending = [s for s in pending if s.id not in ready_ids]
    return ordered


def validate_edge_schemas(stages: list[Stage]) -> list[str]:
    """Raises unless `validate_inputs_resolve` and `validate_publish_is_terminal` ran and passed."""
    by_id = {s.id: s for s in stages}
    issues: list[str] = []
    for stage in stages:
        for ref in stage.inputs:
            input_table_schema = ref.table_schema
            upstream = by_id.get(ref.id)
            if upstream is None:
                raise ValueError(
                    f"`{stage.id}`: input `{ref.id}` references no stage — "
                    "validate_inputs_resolve must run, and pass, before this check"
                )
            upstream_output = upstream.resolve_output_schema()
            if upstream_output is None:
                raise ValueError(
                    f"`{stage.id}`: input `{ref.id}` resolves no output schema — "
                    "publish is the only type exempt, and "
                    "validate_publish_is_terminal must run, and pass, before this check"
                )
            for reason in input_table_schema.find_unsatisfied_columns(upstream_output):
                issues.append(f"`{stage.id}`: input from `{ref.id}` — {reason}")
    return issues


def validate_publish_is_terminal(stages: list[Stage]) -> list[str]:
    publish_ids = {s.id for s in stages if s.type == StageType.publish}
    return [
        f"`{stage.id}`: input `{upstream}` is a publish stage — a publish stage "
        f"writes files and produces no table, so it cannot be another stage's input"
        for stage in stages
        for upstream in stage.input_ids
        if upstream in publish_ids
    ]


def find_stages_reaching_publish(stages: Sequence[Stage]) -> set[str]:
    inputs_of = {stage.id: stage.input_ids for stage in stages}
    reaching: set[str] = set()
    # Walked BACKWARD from the publish stages along `input_ids`, so a stage any number
    # of hops upstream is found. A publish stage is terminal (validate_publish_is_terminal),
    # so its ancestors are exactly the stages whose work the published files carry.
    #
    # Seeded from those stages' INPUTS, so a publish stage is not in the set it roots: it
    # does not carry work into the publishing, it IS the publishing, and narrates itself.
    # Nothing may read a publish stage, so the walk cannot reach one and add it back.
    frontier = [
        upstream
        for stage in stages if stage.type == StageType.publish
        for upstream in stage.input_ids
    ]
    while frontier:
        stage_id = frontier.pop()
        if stage_id in reaching:
            continue
        reaching.add(stage_id)
        # Indexed, not `.get`: an id that resolves to no stage is a broken graph
        # parse_workflow already refuses, and silently treating it as a dead end
        # would UNDER-report reachability — the unsafe direction for a guard.
        frontier.extend(inputs_of[stage_id])
    return reaching


def graph_issues(stages: list[Stage]) -> list[str]:
    edge_check_prerequisites = (
        validate_inputs_resolve(stages) + validate_publish_is_terminal(stages)
    )
    issues = (
        validate_unique_ids(stages) + detect_cycle(stages) + edge_check_prerequisites
    )
    # validate_edge_schemas RAISES on an edge these two report on, so it runs only once
    # they come back clean.
    if edge_check_prerequisites:
        return issues
    return issues + validate_edge_schemas(stages)


class Workflow(_Base):
    stages: list[Stage]

    @model_validator(mode="after")
    def _validate_graph(self) -> "Workflow":
        issues = graph_issues(self.stages)
        if issues:
            raise ValueError("; ".join(issues))
        return self

    def index_stages_by_id(self) -> dict[str, Stage]:
        return {stage.id: stage for stage in self.stages}


def parse_workflow(stages: list[dict[str, Any]]) -> Workflow:
    return Workflow.model_validate({"stages": list(stages)})


def validate_workflow(stages: list[Stage]) -> list[str]:
    return graph_issues(stages)


def validate_workflow_draft(stages: list[dict[str, Any]]) -> list[str]:
    try:
        Workflow.model_validate({"stages": list(stages)})
        return []
    except ValidationError as err:
        return format_errors(err)
