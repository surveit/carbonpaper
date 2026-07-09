"""Does an eval config still fit a methodology's stages?

An eval binds to stages by name and by declared output schema: the tables it
injects must be valid stand-ins for the overridden stages' outputs, and the
columns it asserts on must exist on the target. This module answers "does that
still hold?" for the stages as they are NOW. The answer is computed on demand
and never stored — a stored flag could drift from the stages it describes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.models import (Column, EvalConfig, EvalRunSettings, Methodology, Stage,
                        TableSchema, resolve_eval_run_settings,
                        validate_methodology_stages)
from app.services.cases_columns import derive_cases_columns

_NUMERIC_TYPES = {"int", "float"}


@dataclass
class CompatibilityReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    settings: EvalRunSettings | None = None


def _columns_covered(injected: TableSchema, required: Sequence[Column],
                     stage_id: str, label: str) -> list[str]:
    """Every column in `required` must be present in `injected` with the same
    type. `stage_id` names the stage `required` came from, for the message."""
    injected_types = {c.name: c.type for c in injected.columns}
    problems: list[str] = []
    for col in required:
        got = injected_types.get(col.name)
        if got is None:
            problems.append(
                f"{label}: injected table lacks column `{col.name}` "
                f"required by stage `{stage_id}`")
        elif got != col.type:
            problems.append(
                f"{label}: column `{col.name}` is `{got}` in the injected table "
                f"but `{col.type}` on stage `{stage_id}`")
    return problems


def _coverage_problems(injected: TableSchema, stage: Stage, label: str) -> list[str]:
    """The injected table stands in for `stage`'s output: every column the stage
    declares must be present in the injected schema with the same type."""
    if stage.output_schema is None:
        return [f"cannot verify {label}: stage `{stage.id}` declares no output schema"]
    return _columns_covered(injected, stage.output_schema.columns, stage.id, label)


def check_eval_compatibility(config: EvalConfig,
                             stages: Sequence[Stage]) -> CompatibilityReport:
    by_id = {s.id: s for s in stages}
    problems: list[str] = []

    # Condition 1: every referenced stage exists.
    referenced = [config.override_stage, config.target_stage,
                  *(ov.stage_id for ov in config.reference_overrides)]
    missing = [sid for sid in referenced if sid not in by_id]
    for sid in missing:
        problems.append(f"stage `{sid}` does not exist in the methodology")
    if missing:
        return CompatibilityReport(ok=False, problems=problems, settings=None)

    # Condition 1b: the target must be reachable from the override, else the
    # override injects into a branch that never feeds the target and the eval
    # is inert. Reference overrides are exempt (they inject side data).
    descendants: set[str] = set()
    stack = [config.override_stage]
    while stack:
        node = stack.pop()
        for s in stages:
            if node in s.input_ids and s.id not in descendants:
                descendants.add(s.id)
                stack.append(s.id)
    if config.target_stage not in descendants:
        problems.append(
            f"target `{config.target_stage}` is not reachable from override "
            f"`{config.override_stage}`; the override would not affect it")

    # Condition 2: every injected table is a valid stand-in. The cases table is
    # only checkable once it exists; a dataless config skips this and is scored
    # for everything else (its file is validated when attached). The override
    # stage's coverage is clash-aware: a check whose target column name
    # collides with one of the override's own output columns expects the
    # injected input at `override.<name>`, not the plain name -- the same
    # renaming `derive_cases_columns` applies when it builds the cases-file
    # schema the form and template use.
    if config.table is not None:
        check_actuals = [exp.actual for exp in config.expected]
        derived = derive_cases_columns(
            by_id[config.override_stage], by_id[config.target_stage], check_actuals)
        problems += derived.override_problems
        problems += _columns_covered(
            config.table.table_schema, derived.injected, config.override_stage, "cases table")
    for ov in config.reference_overrides:
        problems += _coverage_problems(
            ov.table.table_schema, by_id[ov.stage_id],
            f"reference override `{ov.stage_id}`")

    # Conditions 3 + 4 need the target's declared output schema.
    target = by_id[config.target_stage]
    if target.output_schema is None:
        problems.append(f"cannot verify assertions: target `{target.id}` "
                        "declares no output schema")
        target_types: dict[str, str] = {}
    else:
        target_types = {c.name: c.type for c in target.output_schema.columns}
        for exp in config.expected:
            got = target_types.get(exp.actual)
            if got is None:
                problems.append(f"expected column asserts on `{exp.actual}`, "
                                f"which target `{target.id}` does not emit")
            elif exp.metric == "abs_tol" and got not in _NUMERIC_TYPES:
                problems.append(f"`{exp.actual}` is `{got}` on target "
                                f"`{target.id}` but metric abs_tol needs a numeric")

    # A reference override on the target stage would make the target its own
    # override, which has no coherent path to resolve — report it rather than
    # letting it reach resolve_eval_run_settings.
    target_collisions = [ov.stage_id for ov in config.reference_overrides
                          if ov.stage_id == config.target_stage]
    for sid in target_collisions:
        problems.append(f"reference override `{sid}` cannot be the target stage")

    # The stage list itself may have cross-stage problems (dangling input,
    # duplicate id, cycle) that a per-file validator wouldn't catch.
    structural_issues = validate_methodology_stages(list(stages))
    if structural_issues:
        problems.append("cannot verify the path: the workflow has structural "
                        "problems: " + "; ".join(structural_issues))

    if target_collisions or structural_issues:
        return CompatibilityReport(ok=False, problems=problems, settings=None)

    # Condition 5: the path must preserve grain, unless a code scorer takes over.
    methodology = Methodology.model_validate(
        {"stages": [s.model_dump(mode="json") for s in stages]})
    overrides = [config.override_stage,
                 *(ov.stage_id for ov in config.reference_overrides)]
    settings = resolve_eval_run_settings(methodology, overrides, config.target_stage)
    if not settings.can_score_declaratively and config.code is None:
        problems.append(
            "not scorable row-by-row (blocking stages: "
            + ", ".join(f"`{b}`" for b in settings.blocking_stages)
            + ") and no code scorer is supplied")

    return CompatibilityReport(ok=not problems, problems=problems, settings=settings)
