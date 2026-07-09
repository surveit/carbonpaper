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

from app.models.schema import _Base, _column_spec_differences, format_errors
from app.models.stage import Stage, StageType


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


def check_llm_transform_one_to_one(stages: list[Stage]) -> list[str]:
    """One issue per llm_transform stage that is not strictly 1:1 on its declared
    schemas. An llm_transform maps one input row to one output row, so it must
    declare exactly one input whose schema and the stage's output_schema both
    name the same primary key, keep every input column unchanged (a transform
    never rewrites an existing column's schema), and add at least one new column.

    Enforcing this here — at save time — means the reply spec the runtime derives
    (`output_schema.subtract(input_schema)`) is exactly the new columns and can
    never throw mid-run, and an ineligible stage is rejected before any run
    rather than during one."""
    issues: list[str] = []
    for s in stages:
        if s.type != StageType.llm_transform:
            continue
        if len(s.inputs) != 1:
            issues.append(
                f"`{s.id}`: llm_transform must have exactly one input, has {len(s.inputs)}"
            )
            continue
        input_schema = s.inputs[0].table_schema
        output_schema = s.output_schema
        if input_schema is None:
            issues.append(f"`{s.id}`: llm_transform's input declares no schema (1:1 needs a primary_key)")
            continue
        if output_schema is None:
            issues.append(f"`{s.id}`: llm_transform declares no output_schema (1:1 needs a primary_key)")
            continue

        input_pk, output_pk = input_schema.primary_key, output_schema.primary_key
        if not input_pk:
            issues.append(f"`{s.id}`: llm_transform's input schema declares no primary_key")
        if not output_pk:
            issues.append(f"`{s.id}`: llm_transform's output_schema declares no primary_key")
        if input_pk and output_pk and set(input_pk) != set(output_pk):
            issues.append(
                f"`{s.id}`: input primary_key {input_pk} != output primary_key "
                f"{output_pk} (llm_transform is strictly 1:1)"
            )

        output_by_name = {c.name: c for c in output_schema.columns}
        for col in input_schema.columns:
            out_col = output_by_name.get(col.name)
            if out_col is None:
                issues.append(
                    f"`{s.id}`: output_schema drops input column `{col.name}` "
                    "(a transform is additive: output ⊇ input)"
                )
                continue
            diffs = _column_spec_differences(col, out_col)
            if diffs:
                issues.append(
                    f"`{s.id}`: column `{col.name}` changes schema on "
                    f"{', '.join(diffs)} between input and output; a transform "
                    "must not modify a column's schema"
                )

        input_names = {c.name for c in input_schema.columns}
        if not any(c.name not in input_names for c in output_schema.columns):
            issues.append(
                f"`{s.id}`: output_schema adds no columns beyond the input; an "
                "llm_transform that adds nothing is a schema bug"
            )
    return issues


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
    """Whole-workflow checks on already-validated stages, as human-readable issue
    strings — every problem, not just the first: the cross-stage graph checks
    (unique ids, inputs resolve, acyclic) plus per-stage semantic invariants
    (llm_transform is strictly 1:1). This is the seam `load_workflow` (and hence
    `create_version`) enforces, so an invalid workflow is never versioned or run."""
    return graph_issues(stages) + check_llm_transform_one_to_one(stages)


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
