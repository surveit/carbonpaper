"""Workflow contract: a workflow is a list of validated stages plus the
cross-stage checks (unique ids, inputs resolve, acyclic).

This module owns ONLY cross-stage checks — the ones that need the whole stage
list to decide. A single stage's own invariants (e.g. an llm_transform being
strictly 1:1) live on the `Stage` model as validators, not here; if a check can
be answered from one stage alone, it does not belong in this file.

The graph checks are plain functions so they can be tested on their own and read
without wading through a validator. Each returns a list of human-readable issue
strings ([] means it found nothing) — the whole batch is collected so one call
surfaces every problem, not just the first.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError, model_validator

from app.models.schema import _Base
from app.models.stage import Stage
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

    An edge is skipped, never flagged, when: the input declares no schema (nothing
    to check); the named upstream stage is missing (`validate_inputs_resolve`
    already reports that — this does not double-report); or the upstream declares
    no `output_schema` (unknowable — a reference we cannot check is never wrong,
    the same rule `Stage._config_columns_resolve` follows)."""
    by_id = {s.id: s for s in stages}
    issues: list[str] = []
    for stage in stages:
        for ref in stage.inputs:
            required = ref.table_schema
            if required is None:
                continue
            upstream = by_id.get(ref.id)
            if upstream is None or upstream.output_schema is None:
                continue
            for reason in required.find_unsatisfied_columns(upstream.output_schema):
                issues.append(f"`{stage.id}`: input from `{ref.id}` — {reason}")
    return issues


def graph_issues(stages: list[Stage]) -> list[str]:
    """Every cross-stage problem in the workflow graph: duplicate ids, dangling
    inputs, a cycle, and any edge whose declared input schema the upstream stage's
    output_schema does not supply. The single source of truth both the strict
    model validator and the non-fatal `validate_workflow` build on."""
    return (
        validate_unique_ids(stages)
        + validate_inputs_resolve(stages)
        + detect_cycle(stages)
        + validate_edge_schemas(stages)
    )


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
    acyclic, and every edge's declared input schema supplied by its upstream
    output_schema. Per-stage invariants (e.g. llm_transform being strictly 1:1) are
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
