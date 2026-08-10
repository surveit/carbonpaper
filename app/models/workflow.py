"""Workflow contract: validated stages plus the cross-stage checks (unique ids, inputs
resolve, acyclic). A check answerable from one stage alone belongs on `Stage`, not here.

Each check returns a list of issue strings ([] means it found nothing), and the whole
batch is collected so one call surfaces every problem, not just the first.
"""
from __future__ import annotations

from typing import Any, Sequence, TypeVar

from pydantic import ValidationError, model_validator

from app.models.schema import TableSchema, _Base
from app.models.stage import (
    Stage,
    StageCommon,
    StageType,
    parse_stage,
    stage_to_spec_dict,
)
from app.core.utils import format_errors

# Ordering and cycle detection read only the shared fields, so they hold a
# submitted draft and a stored stage alike, and hand back what they were given.
_StageT = TypeVar("_StageT", bound=StageCommon)


def validate_unique_ids(stages: Sequence[StageCommon]) -> list[str]:
    """One issue per stage id that appears more than once."""
    ids = [s.id for s in stages]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    return [f"duplicate stage id `{d}`" for d in dupes]


def validate_inputs_resolve(stages: list[Stage]) -> list[str]:
    """One issue per input that names no existing stage — all of them, so a
    reviewer fixes every dangling edge in one pass rather than one per re-run."""
    ids = {s.id for s in stages}
    issues: list[str] = []
    for s in stages:
        for upstream in s.input_ids:
            if upstream not in ids:
                issues.append(f"`{s.id}`: input `{upstream}` references no stage")
    return issues


def detect_cycle(stages: Sequence[StageCommon]) -> list[str]:
    """A one-item list naming the first cycle found, or [] if acyclic. One cycle
    is enough to reject the workflow; we don't enumerate them all. The stage graph
    must stay acyclic — a cycle means the runner could never order the stages.

    An input naming an id outside `stages` is not an edge here, so this finds
    cycles WITHIN the given set only."""
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
    """`stages` reordered so every stage follows the stages it names in `inputs`.
    An input naming an id outside `stages` imposes no order — it is already in the
    workflow, or missing, which is a validation problem and not this function's.
    Ties keep submission order. Ids must be unique (`validate_unique_ids`);
    raises ValueError on a cycle, which `detect_cycle` reports far better."""
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
    """One issue per workflow edge whose declared input schema the upstream stage
    does not supply. `inputs[i].schema` is a REQUIREMENT — possibly a projection
    naming only the columns the stage consumes — that the upstream's
    resolved output schema must subsume (matching spec, compatible nullability, not
    identity; see `TableSchema.find_unsatisfied_columns`). Reports every offending
    column across every edge, so one pass surfaces them all.

    A narrower edge therefore passes. It does not survive into a version —
    `rewrite_input_schemas_from_upstream` rewrites every edge at save — but a
    working copy or a mid-edit draft carries whatever was authored, which is
    what this check governs.

    Raises if an input dangles or its upstream resolves no output schema:
    `validate_inputs_resolve` and `validate_publish_is_terminal` must run, and
    pass, before this check (as `graph_issues` does)."""
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


def rewrite_input_schemas_from_upstream(stages: Sequence[Stage]) -> list[Stage]:
    """`stages` in the order given, every `inputs[].schema` rewritten from its upstream."""
    rewritten: dict[str, Stage] = {}
    # Dependency order, because a stage's own output can BE its first input
    # extended (form `extends`): the upstream must already carry a fresh schema
    # when the stage reading it is asked what IT produces, or one stale edge
    # walks the whole chain. Unique ids and an acyclic graph are
    # sort_stages_by_dependency's preconditions — validate_unique_ids and
    # detect_cycle report a breach of either far better than this can.
    for stage in sort_stages_by_dependency(stages):
        rewritten[stage.id] = _rewrite_one_stage(stage, rewritten)
    result = [rewritten[stage.id] for stage in stages]
    # An edge left stale here is OUR bug, not the author's: the loop above just
    # wrote every one it could resolve, so a mismatch means the rewrite missed.
    unrewritten = _find_unrewritten_input_schemas(result)
    assert not unrewritten, "; ".join(unrewritten)
    return result


def _rewrite_one_stage(stage: Stage, upstream_by_id: dict[str, Stage]) -> Stage:
    """This stage carrying its upstreams' current output schemas; itself when none moved."""
    spec = stage_to_spec_dict(stage)
    changed = False
    for entry, ref in zip(spec["inputs"], stage.inputs):
        produced = _resolve_output_schema_spec(upstream_by_id.get(ref.id))
        # Left exactly as authored where no upstream in this set answers for the
        # edge, or where the upstream emits files rather than a table (publish):
        # there is nothing to copy, and inventing a schema would be fabrication.
        if produced is None or entry["schema"] == produced:
            continue
        entry["schema"] = produced
        changed = True
    # Re-parsed rather than model_copy'd: a rewritten edge must face every stage
    # validator (_signature_consistent, _config_columns_resolve, _schemas_declared)
    # exactly as an authored one does, so a rewrite the stage cannot survive raises.
    return parse_stage(spec) if changed else stage


def _find_unrewritten_input_schemas(stages: Sequence[Stage]) -> list[str]:
    """One entry per edge still caching something other than what its upstream resolves."""
    by_id = {stage.id: stage for stage in stages}
    return [
        f"`{stage.id}`: input from `{ref.id}` caches a schema its upstream does not produce"
        for stage in stages
        for ref in stage.inputs
        if (produced := _resolve_output_schema_spec(by_id.get(ref.id))) is not None
        and _dump_schema_spec(ref.table_schema) != produced
    ]


def _resolve_output_schema_spec(upstream: Stage | None) -> dict[str, Any] | None:
    """What `upstream` promises, in spec-dict form; None when it is absent or emits no table."""
    if upstream is None:
        return None
    produced = upstream.resolve_output_schema()
    return None if produced is None else _dump_schema_spec(produced)


def _dump_schema_spec(table_schema: TableSchema) -> dict[str, Any]:
    # The shape stage_to_spec_dict nests, so the two compare as written and stored.
    return table_schema.model_dump(mode="json", by_alias=True, exclude_none=True)


def validate_publish_is_terminal(stages: list[Stage]) -> list[str]:
    """One issue per edge whose upstream is a publish stage. A publish stage writes
    files instead of producing a table, so nothing downstream can read it. Reports
    every offending edge, not just the first."""
    publish_ids = {s.id for s in stages if s.type == StageType.publish}
    return [
        f"`{stage.id}`: input `{upstream}` is a publish stage — a publish stage "
        f"writes files and produces no table, so it cannot be another stage's input"
        for stage in stages
        for upstream in stage.input_ids
        if upstream in publish_ids
    ]


def find_stages_reaching_publish(stages: Sequence[Stage]) -> set[str]:
    """Ids of the publish stages' ANCESTORS — the stages whose work the published files carry."""
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
    """Every cross-stage problem in the workflow graph: duplicate ids, dangling
    inputs, a cycle, an edge reading a publish stage, and any edge whose declared
    input schema the upstream stage's resolved output does not supply. The single
    source of truth both the strict
    model validator and the non-fatal `validate_workflow` build on."""
    # validate_edge_schemas raises rather than reports on an edge it cannot check:
    # an input naming no stage, or an upstream resolving no output (only publish
    # is exempt). Both are reportable findings of the two checks below, so it runs
    # only once they pass.
    edge_check_prerequisites = (
        validate_inputs_resolve(stages) + validate_publish_is_terminal(stages)
    )
    issues = (
        validate_unique_ids(stages) + detect_cycle(stages) + edge_check_prerequisites
    )
    if edge_check_prerequisites:
        return issues
    return issues + validate_edge_schemas(stages)


class Workflow(_Base):
    """A whole workflow: validated stages with unique ids, resolvable inputs, acyclic."""
    stages: list[Stage]

    @model_validator(mode="after")
    def _validate_graph(self) -> "Workflow":
        issues = graph_issues(self.stages)
        if issues:
            raise ValueError("; ".join(issues))
        return self

    def index_stages_by_id(self) -> dict[str, Stage]:
        """This workflow's stages keyed by id, for callers that need repeated
        by-id lookup. Unique ids are already a graph invariant (see
        `validate_unique_ids`), so this never collapses two stages onto one
        key."""
        return {stage.id: stage for stage in self.stages}


def parse_workflow(stages: list[dict[str, Any]]) -> Workflow:
    """Parse + validate a list of stage dicts. Raises ValidationError if invalid."""
    return Workflow.model_validate({"stages": list(stages)})


def validate_workflow(stages: list[Stage]) -> list[str]:
    """Cross-stage checks on already-parsed stages, as human-readable issue
    strings — every problem, not just the first: unique ids, inputs resolve,
    acyclic, no stage reading a publish stage, and every edge's declared input
    schema supplied by its upstream output_schema. Per-stage invariants (e.g. llm_transform being strictly 1:1) are
    already enforced by `Stage` construction, so any `list[Stage]` reaching here
    is stage-valid; this is the remaining, whole-graph seam `load_workflow` (and
    hence `save_working_copy_as_version`) enforces, so an invalid workflow is never versioned
    or run."""
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
