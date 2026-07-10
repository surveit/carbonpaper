"""Does an eval config still fit a workflow's stages?

An eval binds to stages by name and by declared output schema: the tables it
injects must be valid stand-ins for the overridden stages' outputs, and the
columns it asserts on must exist on the target. This module answers "does that
still hold?" for the stages as they are NOW. The answer is computed on demand
and never stored — a stored flag could drift from the stages it describes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.models import (EvalConfig, EvalRunSettings, Stage, TableSchema,
                        Workflow, resolve_eval_run_settings, validate_workflow)
from app.services.cases_columns import derive_cases_columns

_NUMERIC_TYPES = {"int", "float"}


@dataclass
class CompatibilityReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    settings: EvalRunSettings | None = None


def check_eval_compatibility(config: EvalConfig,
                             stages: Sequence[Stage]) -> CompatibilityReport:
    """Does `config` still fit `stages` as they are NOW? Binds by stage id
    and by declared output schema; the answer is computed fresh from the
    stages every time, never stored, so it can't drift from what they
    actually declare. Runs every check and collects every problem in one
    pass, except where an earlier failure makes the rest unanswerable: a
    missing stage, a target-colliding reference override, or a structurally
    broken workflow all short-circuit before the path (and so `settings`) is
    even attempted."""
    by_id = {s.id: s for s in stages}

    missing = _missing_stage_problems(config, by_id)
    if missing:
        return CompatibilityReport(ok=False, problems=missing, settings=None)

    problems = (
        _reachability_problems(config, stages)
        + _cases_coverage_problems(config, by_id)
        + _reference_override_problems(config, by_id)
        + _target_check_problems(config, by_id)
    )
    gate_problems, blocked = _structural_gate_problems(config, stages)
    problems += gate_problems
    if blocked:
        return CompatibilityReport(ok=False, problems=problems, settings=None)

    settings, grain_problems = _grain_settings(config, stages)
    problems += grain_problems
    return CompatibilityReport(ok=not problems, problems=problems, settings=settings)


# ── Condition 1: every referenced stage exists ────────────────────────────────
def _missing_stage_problems(config: EvalConfig, by_id: dict[str, Stage]) -> list[str]:
    """Every stage id the config references (override, target, each reference
    override) must exist — checked first since nothing else here is
    answerable against a stage that isn't there."""
    referenced = [config.override_stage, config.target_stage,
                 *(ov.stage_id for ov in config.reference_overrides)]
    return [f"stage `{sid}` does not exist in the workflow"
           for sid in referenced if sid not in by_id]


# ── Condition 1b: the override must actually reach the target ────────────────
def _reachability_problems(config: EvalConfig, stages: Sequence[Stage]) -> list[str]:
    """The target must be reachable from the override, else the override
    injects into a branch that never feeds the target and the eval is inert.
    Reference overrides are exempt (they inject side data)."""
    if config.target_stage in _descendants(config.override_stage, stages):
        return []
    return [f"target `{config.target_stage}` is not reachable from override "
           f"`{config.override_stage}`; the override would not affect it"]


def _descendants(stage_id: str, stages: Sequence[Stage]) -> set[str]:
    descendants: set[str] = set()
    stack = [stage_id]
    while stack:
        node = stack.pop()
        for s in stages:
            if node in s.input_ids and s.id not in descendants:
                descendants.add(s.id)
                stack.append(s.id)
    return descendants


# ── Condition 2: every injected table is a valid stand-in ────────────────────
def _cases_coverage_problems(config: EvalConfig, by_id: dict[str, Stage]) -> list[str]:
    """The cases table, if attached, must be a valid stand-in for the
    override stage's output: every column `derive_cases_columns` says the
    file must carry (clash-renamed) has to spec-match a column already in
    it. A dataless config skips this — its file is validated when attached."""
    if config.table is None:
        return []
    check_actuals = [exp.actual for exp in config.expected]
    derived = derive_cases_columns(
        by_id[config.override_stage], by_id[config.target_stage], check_actuals)
    required = TableSchema(columns=derived.injected)
    return derived.override_problems + _missing_columns_problems(
        required, config.table.table_schema, config.override_stage, "cases table")


def _reference_override_problems(config: EvalConfig, by_id: dict[str, Stage]) -> list[str]:
    """Each reference override's injected table must be a valid stand-in for
    the stage it overrides — same coverage requirement as the cases table,
    applied per reference override."""
    problems: list[str] = []
    for ov in config.reference_overrides:
        stage = by_id[ov.stage_id]
        if stage.output_schema is None:
            problems.append(f"cannot verify reference override `{ov.stage_id}`: "
                            f"stage declares no output schema")
            continue
        problems += _missing_columns_problems(
            stage.output_schema, ov.table.table_schema, ov.stage_id,
            f"reference override `{ov.stage_id}`")
    return problems


def _missing_columns_problems(required: TableSchema, provided: TableSchema,
                              stage_id: str, label: str) -> list[str]:
    """Every column `required` has that `provided` doesn't spec-match, via
    `TableSchema.missing_from` — absent by name or differing on type,
    nullability, or another spec field (prose aside)."""
    return [f"{label}: injected table lacks (or mismatches) column `{col.name}` "
           f"required by stage `{stage_id}`"
           for col in required.missing_from(provided)]


# ── Conditions 3 + 3b: the target's declared output covers the checks ────────
def _target_check_problems(config: EvalConfig, by_id: dict[str, Stage]) -> list[str]:
    """Every check's `actual` column must exist on the target's declared
    output, and an abs_tol check needs that column to be numeric."""
    target = by_id[config.target_stage]
    if target.output_schema is None:
        return [f"cannot verify assertions: target `{target.id}` declares no output schema"]
    problems: list[str] = []
    for exp in config.expected:
        col = target.output_schema.column(exp.actual)
        if col is None:
            problems.append(f"expected column asserts on `{exp.actual}`, "
                            f"which target `{target.id}` does not emit")
        elif exp.metric == "abs_tol" and col.type not in _NUMERIC_TYPES:
            problems.append(f"`{exp.actual}` is `{col.type}` on target "
                            f"`{target.id}` but metric abs_tol needs a numeric")
    return problems


# ── Condition 4: a reference override cannot also be the target ──────────────
def _target_collision_problems(config: EvalConfig) -> list[str]:
    """A reference override on the target stage would make the target its
    own override, which has no coherent path to resolve — reported here
    rather than letting it reach `resolve_eval_run_settings`."""
    return [f"reference override `{ov.stage_id}` cannot be the target stage"
           for ov in config.reference_overrides if ov.stage_id == config.target_stage]


# ── Structural: the stage list itself must be a valid workflow ───────────────
def _structural_problems(stages: Sequence[Stage]) -> list[str]:
    """Cross-stage problems (dangling input, duplicate id, cycle) a per-file
    validator wouldn't catch."""
    issues = validate_workflow(list(stages))
    if not issues:
        return []
    return ["cannot verify the path: the workflow has structural problems: "
           + "; ".join(issues)]


def _structural_gate_problems(config: EvalConfig,
                              stages: Sequence[Stage]) -> tuple[list[str], bool]:
    """Condition 4 + structural combined, with whether either blocks going
    further: both leave the path unresolvable, so `settings` can't even be
    attempted."""
    problems = _target_collision_problems(config) + _structural_problems(stages)
    return problems, bool(problems)


# ── Condition 5: the path must preserve grain (or fall back to a code scorer) ─
def _grain_settings(config: EvalConfig,
                    stages: Sequence[Stage]) -> tuple[EvalRunSettings, list[str]]:
    """The path must preserve grain row-by-row, unless a code scorer takes
    over — the same decision `resolve_eval_run_settings` makes for a run."""
    workflow = Workflow.model_validate({"stages": [s.model_dump(mode="json") for s in stages]})
    overrides = [config.override_stage,
                *(ov.stage_id for ov in config.reference_overrides)]
    settings = resolve_eval_run_settings(workflow, overrides, config.target_stage)
    if settings.can_score_declaratively or config.code is not None:
        return settings, []
    return settings, [
        "not scorable row-by-row (blocking stages: "
        + ", ".join(f"`{b}`" for b in settings.blocking_stages)
        + ") and no code scorer is supplied"]
