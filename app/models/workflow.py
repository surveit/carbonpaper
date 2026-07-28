"""Workflow contract: validated stages plus the cross-stage checks (unique ids, inputs
resolve, acyclic). A check answerable from one stage alone belongs on `Stage`, not here.

Each check returns a list of issue strings ([] means it found nothing), and the whole
batch is collected so one call surfaces every problem, not just the first.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError, model_validator

from app.models.schema import _Base
from app.models.stage import Stage, StageType
from app.core.utils import format_errors


def validate_unique_ids(stages: list[Stage]) -> list[str]:
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


def validate_edge_schemas(stages: list[Stage]) -> list[str]:
    """One issue per workflow edge whose declared input schema the upstream stage
    does not supply. A downstream stage may declare, on each input, the schema it
    expects that upstream to satisfy (`inputs[i].schema`). That declaration is a
    REQUIREMENT — possibly a projection naming only the columns the stage consumes
    — and every column it names must appear in the upstream stage's `output_schema`
    with a matching spec and compatible nullability (subsumption, not identity;
    see `TableSchema.find_unsatisfied_columns`). Reports every offending column
    across every edge, so one pass surfaces them all.

    Every input must already resolve to a stage — call `validate_inputs_resolve`
    first (as `graph_issues` does) or this raises. A `publish` upstream is skipped:
    it is the one type exempt from declaring an output_schema, and nothing forbids
    it being another stage's input today."""
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
            if upstream.output_schema is None:
                continue
            for reason in input_table_schema.find_unsatisfied_columns(upstream.output_schema):
                issues.append(f"`{stage.id}`: input from `{ref.id}` — {reason}")
    return issues


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


def graph_issues(stages: list[Stage]) -> list[str]:
    """Every cross-stage problem in the workflow graph: duplicate ids, dangling
    inputs, a cycle, an edge reading a publish stage, and any edge whose declared
    input schema the upstream stage's output_schema does not supply. The single
    source of truth both the strict
    model validator and the non-fatal `validate_workflow` build on."""
    dangling = validate_inputs_resolve(stages)
    issues = (
        validate_unique_ids(stages)
        + dangling
        + detect_cycle(stages)
        + validate_publish_is_terminal(stages)
    )
    if dangling:
        # validate_edge_schemas raises on an input naming no stage: it may only
        # run once every input resolves.
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
    hence `create_version_from_disk`) enforces, so an invalid workflow is never versioned
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
