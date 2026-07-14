"""Does an eval config still fit a workflow's stages?

An eval binds to stages by name and by declared output schema: the tables it
injects must be valid stand-ins for the overridden stages' outputs, and the
columns it asserts on must exist on the target. This module answers "does that
still hold?" for the stages as they are NOW. The answer is computed on demand
and never stored — a stored flag could drift from the stages it describes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.core.models import (EvalConfig, EvalRunSettings, Stage, TableSchema,
                        Workflow, validate_workflow)
from app.evals.dataset_columns import get_injected_columns
from app.evals.run_settings import resolve_eval_run_settings

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
    actually declare.

    Two phases. First, the preconditions the coverage checks and
    `_resolve_grain_settings` need to be answerable at all: every referenced
    stage exists, the target is reachable, the target emits every checked
    column, the override declares an output schema, no reference override
    targets the target stage itself, and the workflow is structurally
    valid. Any failure here short-circuits with `settings=None` -- in
    particular, `_check_eval_dataset_covers_override` calls into
    `app.evals.dataset_columns`, which raises rather than silently
    degrading on a missing output schema or an unresolvable checked column,
    so those preconditions must hold before it's reached. Second, once
    preconditions hold, the coverage checks and grain resolution run and
    are reported together."""
    by_id = {s.id: s for s in stages}

    missing = _check_stages_exist(config, by_id)
    if missing:
        return CompatibilityReport(ok=False, problems=missing, settings=None)

    precondition_problems = (
        _check_target_reachable(config, stages)
        + _check_target_emits_checked_columns(config, by_id)
        + _check_override_declares_output_schema(config, by_id)
        + _check_no_reference_override_on_target(config)
        + _check_workflow_structure(stages)
    )
    if precondition_problems:
        return CompatibilityReport(ok=False, problems=precondition_problems, settings=None)

    problems = (
        _check_eval_dataset_covers_override(config, by_id)
        + _check_reference_overrides_cover_stages(config, by_id)
    )
    settings, grain_problems = _resolve_grain_settings(config, stages)
    problems += grain_problems
    return CompatibilityReport(ok=not problems, problems=problems, settings=settings)


# ── Condition 1: every referenced stage exists ────────────────────────────────
def _check_stages_exist(config: EvalConfig, by_id: dict[str, Stage]) -> list[str]:
    """Every stage id the config references (override, target, each reference
    override) must exist — checked first since nothing else here is
    answerable against a stage that isn't there."""
    referenced = [config.override_stage, config.target_stage,
                 *(ov.stage_id for ov in config.reference_overrides)]
    return [f"stage `{sid}` does not exist in the workflow"
           for sid in referenced if sid not in by_id]


# ── Condition 1b: the override must actually reach the target ────────────────
def _check_target_reachable(config: EvalConfig, stages: Sequence[Stage]) -> list[str]:
    """The target must be reachable from the override, else the override
    injects into a branch that never feeds the target and the eval is inert.
    Reference overrides are exempt (they inject side data)."""
    if config.target_stage in _find_descendants(config.override_stage, stages):
        return []
    return [f"target `{config.target_stage}` is not reachable from override "
           f"`{config.override_stage}`; the override would not affect it"]


def _find_descendants(stage_id: str, stages: Sequence[Stage]) -> set[str]:
    """Every stage reachable downstream of `stage_id`. Bounded regardless of
    a cycle in `stages`: `descendants` is a visited set checked BEFORE a node
    is pushed, so each stage id is pushed at most once and the walk always
    terminates."""
    descendants: set[str] = set()
    stack = [stage_id]
    while stack:
        node = stack.pop()
        for s in stages:
            if node in s.input_ids and s.id not in descendants:
                descendants.add(s.id)
                stack.append(s.id)
    return descendants


# ── Precondition: the override must declare an output schema ─────────────────
def _check_override_declares_output_schema(config: EvalConfig,
                                           by_id: dict[str, Stage]) -> list[str]:
    """The eval-dataset table, if attached, stands in for the override
    stage's declared output, so the override must actually declare one
    before `_check_eval_dataset_covers_override` can resolve required
    columns against it. A dataless config has no file to stand in for, so
    it's exempt."""
    if config.table is None:
        return []
    override = by_id[config.override_stage]
    if override.output_schema is None:
        return [f"override stage `{override.id}` declares no output schema"]
    return []


# ── Condition 2: every injected table is a valid stand-in ────────────────────
def _check_eval_dataset_covers_override(config: EvalConfig, by_id: dict[str, Stage]) -> list[str]:
    """The eval-dataset table, if attached, must be a valid stand-in for the
    override stage's output: every column `get_injected_columns` says the
    file must carry (deconflicted) has to spec-match a column already in it.
    A dataless config skips this — its file is validated when attached.
    Preconditions (override declares an output schema; every checked column
    resolves against the target) are verified by the caller before this
    runs."""
    if config.table is None:
        return []
    override = by_id[config.override_stage]
    check_output_columns = [expected_output.output_column for expected_output in config.expected_outputs]
    required = TableSchema(columns=get_injected_columns(
        override, by_id[config.target_stage], check_output_columns))
    return _check_columns_covered(
        required, config.table.table_schema, config.override_stage, "eval-dataset table")


def _check_reference_overrides_cover_stages(config: EvalConfig,
                                            by_id: dict[str, Stage]) -> list[str]:
    """Each reference override's injected table must be a valid stand-in for
    the stage it overrides — same coverage requirement as the eval-dataset
    table, applied per reference override."""
    problems: list[str] = []
    for ov in config.reference_overrides:
        stage = by_id[ov.stage_id]
        if stage.output_schema is None:
            problems.append(f"cannot verify reference override `{ov.stage_id}`: "
                            f"stage declares no output schema")
            continue
        problems += _check_columns_covered(
            stage.output_schema, ov.table.table_schema, ov.stage_id,
            f"reference override `{ov.stage_id}`")
    return problems


def _check_columns_covered(required: TableSchema, provided: TableSchema,
                           stage_id: str, label: str) -> list[str]:
    """Every column `required` has that `provided` doesn't spec-match, via
    `TableSchema.subtract(strict=False)` — absent by name or differing on
    type, nullability, or another spec field (prose aside)."""
    return [f"{label}: injected table lacks (or mismatches) column `{col.name}` "
           f"required by stage `{stage_id}`"
           for col in required.subtract(provided, strict=False).columns]


# ── Conditions 3 + 3b: the target's declared output covers the checks ────────
def _check_target_emits_checked_columns(config: EvalConfig,
                                        by_id: dict[str, Stage]) -> list[str]:
    """Every check's `output_column` must exist on the target's declared
    output, and an abs_tol check needs that column to be numeric."""
    target = by_id[config.target_stage]
    if target.output_schema is None:
        return [f"cannot verify assertions: target `{target.id}` declares no output schema"]
    problems: list[str] = []
    for expected_output in config.expected_outputs:
        col = target.output_schema.column_for_name(expected_output.output_column)
        if col is None:
            problems.append(f"expected output asserts on `{expected_output.output_column}`, "
                            f"which target `{target.id}` does not emit")
        elif expected_output.metric == "abs_tol" and col.type not in _NUMERIC_TYPES:
            problems.append(f"`{expected_output.output_column}` is `{col.type}` on target "
                            f"`{target.id}` but metric abs_tol needs a numeric")
    return problems


# ── Condition 4: a reference override cannot also be the target ──────────────
def _check_no_reference_override_on_target(config: EvalConfig) -> list[str]:
    """A reference override on the target stage would make the target its
    own override, which has no coherent path to resolve — reported here
    rather than letting it reach `resolve_eval_run_settings`."""
    return [f"reference override `{ov.stage_id}` cannot be the target stage"
           for ov in config.reference_overrides if ov.stage_id == config.target_stage]


# ── Structural: the stage list itself must be a valid workflow ───────────────
def _check_workflow_structure(stages: Sequence[Stage]) -> list[str]:
    """Cross-stage problems (dangling input, duplicate id, cycle) a per-file
    validator wouldn't catch."""
    issues = validate_workflow(list(stages))
    if not issues:
        return []
    return ["cannot verify the path: the workflow has structural problems: "
           + "; ".join(issues)]


# ── Condition 5: the path must preserve grain (or fall back to a code scorer) ─
def _resolve_grain_settings(config: EvalConfig,
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
