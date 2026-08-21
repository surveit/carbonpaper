"""Workflow contract: validated stages, the cross-stage checks (unique ids, inputs
resolve, acyclic), and schema resolution — a stage's input and output schemas are a
function of the whole graph, so they are computed here and handed out as
`WorkflowStage`. A check answerable from one stage alone belongs on `Stage`, and
each returns a list of issue strings, [] meaning it found nothing."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Any, Mapping, Optional, Protocol, Sequence, TypeVar

from pydantic import ValidationError, model_validator

from app.models.schema import (
    StageId,
    TableSchema,
    TypeUnsafeUserStageConfigOverride,
    _Base,
)
from app.models.stage import Stage, StageType, find_cache_ignored_reason
from app.models.stages.input_data import Connector, InputDataStage
from app.models.stages.signature import find_signature_issues, promised_output_schema
from app.models.workflow_stage import WorkflowStage, WorkflowStageInput
from app.core.utils import format_errors
from app.core.ids import ID


# Ordering and cycle detection read a stage's id and its upstream ids and nothing
# else, so they hold a submitted draft and a stored stage alike, and hand back what
# they were given.
class StageInGraph(Protocol):
    id: ID

    @property
    def input_ids(self) -> list[ID]: ...


_StageT = TypeVar("_StageT", bound=StageInGraph)


def validate_unique_ids(stages: Sequence[StageInGraph]) -> list[str]:
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


def detect_cycle(stages: Sequence[StageInGraph]) -> list[str]:
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
    # Resolution RAISES on anything these report, so it runs only once they come
    # back clean.
    structural = (
        validate_unique_ids(stages) + detect_cycle(stages)
        + validate_inputs_resolve(stages) + validate_publish_is_terminal(stages)
    )
    if structural:
        return structural
    return _find_resolution_issues(stages)


def resolve_workflow_stages(stages: list[Stage]) -> list[WorkflowStage]:
    """Raises unless `graph_issues` ran and came back clean."""
    outputs: dict[str, Optional[TableSchema]] = {}
    resolved: dict[str, WorkflowStage] = {}
    for stage in sort_stages_by_dependency(stages):
        inputs = _resolve_inputs(stage, outputs)
        output_schema = promised_output_schema(stage, inputs)
        outputs[stage.id] = output_schema
        resolved[stage.id] = WorkflowStage(
            stage=stage, inputs=inputs, output_schema=output_schema
        )
    return [resolved[stage.id] for stage in stages]


def _find_resolution_issues(stages: list[Stage]) -> list[str]:
    outputs: dict[str, Optional[TableSchema]] = {}
    for stage in sort_stages_by_dependency(stages):
        inputs = _resolve_inputs(stage, outputs)
        issues = _find_workflow_stage_issues(stage, inputs)
        if issues:
            # Its output is in doubt, and every stage downstream resolves through
            # it — so the walk stops here rather than reporting what follows from
            # a schema this stage may not actually emit.
            return issues
        outputs[stage.id] = promised_output_schema(stage, inputs)
    return []


def _find_workflow_stage_issues(
    stage: Stage, inputs: list[WorkflowStageInput]
) -> list[str]:
    return (
        find_signature_issues(stage, inputs)
        + stage.find_config_column_issues(inputs)
        + stage.find_signature_schema_issues(inputs)
    )


def _resolve_inputs(
    stage: Stage, outputs: dict[str, Optional[TableSchema]]
) -> list[WorkflowStageInput]:
    return [
        WorkflowStageInput(
            id=ref.id, table_schema=_require_upstream_output(stage, ref.id, outputs)
        )
        for ref in stage.inputs
    ]


def _require_upstream_output(
    stage: Stage, upstream_id: ID, outputs: dict[str, Optional[TableSchema]]
) -> TableSchema:
    if upstream_id not in outputs:
        raise ValueError(
            f"`{stage.id}`: input `{upstream_id}` references no stage — "
            "validate_inputs_resolve must run, and pass, before resolution"
        )
    output_schema = outputs[upstream_id]
    if output_schema is None:
        raise ValueError(
            f"`{stage.id}`: input `{upstream_id}` resolves no output schema — "
            "publish is the only type exempt, and validate_publish_is_terminal "
            "must run, and pass, before resolution"
        )
    return output_schema


class Workflow(_Base):
    stages: list[Stage]

    @model_validator(mode="after")
    def _validate_graph(self) -> "Workflow":
        issues = graph_issues(self.stages)
        if issues:
            raise ValueError("; ".join(issues))
        return self

    def apply_run_bindings(
        self, bindings: Mapping[StageId, TypeUnsafeUserStageConfigOverride] | None
    ) -> tuple["Workflow", dict[StageId, str]]:
        """A binding reaches `connector.params` only, so every resolved schema survives it."""
        given = dict(bindings or {})
        connector_ids = {s.id for s in self.stages if isinstance(s, InputDataStage)}
        _refuse_unbindable_stage_ids(given, connector_ids)
        rebound = [
            _merge_connector_params(stage, given[stage.id])
            # `given`'s keys were just checked to be connector_ids, which is exactly
            # the input_data stages — so the isinstance never rejects a bound stage.
            if isinstance(stage, InputDataStage) and stage.id in given
            else stage
            for stage in self.stages
        ]
        sources = {sid: "run" if sid in given else "workflow" for sid in connector_ids}
        return Workflow(stages=rebound), sources

    def list_workflow_stages(self) -> list[WorkflowStage]:
        return self._resolved_stages

    def find_workflow_stage(self, stage_id: ID) -> WorkflowStage:
        workflow_stage = self.index_workflow_stages_by_id().get(stage_id)
        if workflow_stage is None:
            raise KeyError(f"no stage `{stage_id}` in this workflow")
        return workflow_stage

    def index_workflow_stages_by_id(self) -> dict[str, WorkflowStage]:
        return {workflow_stage.id: workflow_stage for workflow_stage in self._resolved_stages}

    def index_stages_by_id(self) -> dict[str, Stage]:
        return {stage.id: stage for stage in self.stages}

    @cached_property
    def _resolved_stages(self) -> list[WorkflowStage]:
        return resolve_workflow_stages(self.stages)


def _refuse_unbindable_stage_ids(
    given: Mapping[StageId, TypeUnsafeUserStageConfigOverride], connector_ids: set[StageId]
) -> None:
    unbindable = sorted(set(given) - connector_ids)
    if unbindable:
        raise ValueError(
            f"bindings target stage id(s) with no connector to bind: {unbindable}; "
            f"bindable stages are {sorted(connector_ids)}")


def _merge_connector_params(
    stage: InputDataStage, binding: TypeUnsafeUserStageConfigOverride
) -> InputDataStage:
    if not isinstance(binding, Mapping):
        raise ValueError(
            f"binding for `{stage.id}` must be a dict of connector params, "
            f"got {type(binding).__name__}: {binding!r}")
    try:
        connector = Connector.model_validate({
            **stage.connector.model_dump(),
            "params": _overridden_params(stage.connector.params, binding),
        })
    except ValidationError as err:
        raise ValueError(f"binding for `{stage.id}` is invalid: {err}") from err
    return stage.model_copy(update={"connector": connector})


def _overridden_params(
    authored: dict[str, Any], binding: Mapping[str, Any]
) -> dict[str, Any]:
    """A None in the binding REMOVES the authored key, so a run can name a param off."""
    merged = {**authored, **binding}
    # Overriding one of two params that contradict each other has to be able to take
    # the other one away; leaving it would hand the model a pair it must refuse.
    return {key: value for key, value in merged.items() if value is not None}


def parse_workflow(stages: list[dict[str, Any]]) -> Workflow:
    return Workflow.model_validate({"stages": list(stages)})


@dataclass(frozen=True)
class WorkflowNotFormed:
    """Never empty: a stage list that forms no workflow carries why it did not."""
    issues: list[str]

    def __post_init__(self) -> None:
        if not self.issues:
            raise ValueError("a workflow that did not form must say why; `issues` is empty")


def build_workflow(stages: list[Stage]) -> Workflow | WorkflowNotFormed:
    try:
        return Workflow(stages=stages)
    except ValidationError as err:
        return WorkflowNotFormed(issues=graph_issues(stages) or format_errors(err))


def validate_workflow(stages: list[Stage]) -> list[str]:
    return graph_issues(stages)


def validate_workflow_draft(stages: list[dict[str, Any]]) -> list[str]:
    """Stricter than loading one: a stored workflow already carrying a dead `cache` runs."""
    try:
        workflow = Workflow.model_validate({"stages": list(stages)})
    except ValidationError as err:
        return format_errors(err)
    return [issue for stage in workflow.stages for issue in find_dead_cache_flag_issues(stage)]


def find_dead_cache_flag_issues(stage: Stage) -> list[str]:
    reason = find_cache_ignored_reason(stage.type)
    if not stage.cache or reason is None:
        return []
    return [
        f"stage '{stage.id}': `cache` is set, but `{stage.type}` never consults one — "
        f"{reason}. Leave it off."
    ]
