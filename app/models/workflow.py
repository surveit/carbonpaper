"""Workflow contract: a workflow is a list of validated stages plus the
cross-stage checks (unique ids, inputs resolve, acyclic) and an edge-schema
conformance check.

This module owns ONLY cross-stage checks — the ones that need the whole stage
list to decide. A single stage's own invariants (e.g. an llm_transform being
strictly 1:1) live on the `Stage` model as validators, not here; if a check can
be answered from one stage alone, it does not belong in this file.

The graph checks are plain functions so they can be tested on their own and read
without wading through a validator. Each returns a list of human-readable issue
strings ([] means it found nothing) — the whole batch is collected so one call
surfaces every problem, not just the first. `check_edge_schemas` is separate: it
returns typed issue objects (not part of the fatal graph validation) and drives
the workflow view's flagged edges.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError, model_validator

from app.models.schema import TableSchema, _Base, format_errors
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


@dataclass
class WorkflowValidationIssue:
    """A problem found validating a workflow at author/save time. Distinct from a
    runtime data-validation failure (`app/runtime/validation.py`'s `Issue`), which
    judges a produced dataframe — this judges the workflow definition itself."""
    problem: str


@dataclass
class EdgeSchemaIssue(WorkflowValidationIssue):
    """A WorkflowValidationIssue on one edge: the downstream stage's declared input
    schema disagrees with (or cannot be checked against) the upstream stage's
    declared output schema."""
    upstream_id: str
    stage_id: str


def check_edge_schemas(stages: list[Stage]) -> list[EdgeSchemaIssue]:
    """One issue per edge whose declared input schema does not conform to the
    upstream stage's declared `output_schema`, tagged with the edge it belongs to.

    Conformance is the subtraction relation: the declared input copy must be a
    spec-preserving projection of the producer's output — it may drop columns, but
    on every column it declares it must match the producer's spec exactly. An input
    declared as a bare id (no `schema:` block) is skipped; an input id that resolves
    to no stage is `check_inputs_resolve`'s concern.
    """
    by_id = {s.id: s for s in stages}
    out: list[EdgeSchemaIssue] = []
    for s in stages:
        for ref in s.inputs:
            if ref.table_schema is None or ref.id not in by_id:
                continue
            out.extend(_edge_issues(ref.id, s.id, ref.table_schema,
                                    by_id[ref.id].output_schema))
    return out


def _edge_issues(upstream_id: str, stage_id: str, in_schema: TableSchema,
                 up_schema: TableSchema | None) -> list[EdgeSchemaIssue]:
    """One issue if `in_schema` (the downstream input copy) does not conform to
    `up_schema` (the upstream output_schema), else none. Conformance is exactly
    subtractability: `up_schema.subtract(in_schema)` succeeds iff the copy is a
    spec-preserving projection of the producer, and raises naming the columns that
    disagree. Also flags the edge-only case of an upstream with no output_schema to
    check against."""
    if up_schema is None or not up_schema.columns:
        return [EdgeSchemaIssue(
            problem="upstream declares no output_schema to check the input copy against",
            upstream_id=upstream_id, stage_id=stage_id)]
    try:
        up_schema.subtract(in_schema)
        return []
    except ValueError as mismatch:
        return [EdgeSchemaIssue(problem=str(mismatch),
                                upstream_id=upstream_id, stage_id=stage_id)]


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
    """Cross-stage checks on already-parsed stages, as human-readable issue
    strings — every problem, not just the first: unique ids, inputs resolve,
    acyclic. Per-stage invariants (e.g. llm_transform being strictly 1:1) are
    already enforced by `Stage` construction, so any `list[Stage]` reaching here
    is stage-valid; this is the remaining, whole-graph seam `load_workflow` (and
    hence `create_version`) enforces, so an invalid workflow is never versioned
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
