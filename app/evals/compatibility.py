"""Does an eval config still fit a workflow's stages, as they are NOW?

The answer is computed on demand and never stored -- a stored flag could drift
from the stages it describes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.models import (EvalConfig, EvalRunSettings, ScoringMetric,
                        TableSchema, Workflow, WorkflowStage)
from app.evals.dataset_columns import get_injected_columns
from app.evals.run_settings import resolve_eval_run_settings

_NUMERIC_TYPES = {"int", "float"}


@dataclass
class CompatibilityReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    settings: EvalRunSettings | None = None


def validate_eval_compatibility(
    config: EvalConfig, workflow: Workflow | None, workflow_issues: Sequence[str]
) -> CompatibilityReport:
    """`workflow_issues` is non-empty exactly when `workflow` is None — it says why."""
    if workflow is None:
        return CompatibilityReport(
            ok=False, problems=_describe_broken_workflow(workflow_issues), settings=None)

    by_id = workflow.index_workflow_stages_by_id()
    missing = _validate_stages_exist(config, by_id)
    if missing:
        return CompatibilityReport(ok=False, problems=missing, settings=None)

    precondition_problems = (
        _validate_target_reachable(config, by_id)
        + _validate_target_emits_checked_columns(config, by_id)
        + _validate_override_declares_output_schema(config, by_id)
        + _validate_no_reference_override_on_target(config)
    )
    if precondition_problems:
        return CompatibilityReport(ok=False, problems=precondition_problems, settings=None)

    problems = (
        _validate_eval_dataset_covers_override(config, by_id)
        + _validate_reference_overrides_cover_stages(config, by_id)
    )
    settings, grain_problems = _resolve_grain_settings(config, workflow)
    problems += grain_problems
    return CompatibilityReport(ok=not problems, problems=problems, settings=settings)


# ── Condition 1: every referenced stage exists ────────────────────────────────
def _validate_stages_exist(config: EvalConfig, by_id: dict[str, WorkflowStage]) -> list[str]:
    referenced = [config.override_stage, config.target_stage,
                 *(ov.stage_id for ov in config.reference_overrides)]
    return [f"stage `{sid}` does not exist in the workflow"
           for sid in referenced if sid not in by_id]


# ── Condition 1b: the override must actually reach the target ────────────────
def _validate_target_reachable(
    config: EvalConfig, by_id: dict[str, WorkflowStage]
) -> list[str]:
    if config.target_stage in _find_descendants(config.override_stage, by_id):
        return []
    return [f"target `{config.target_stage}` is not reachable from override "
           f"`{config.override_stage}`; the override would not affect it"]


def _find_descendants(stage_id: str, by_id: dict[str, WorkflowStage]) -> set[str]:
    """Terminates on a cyclic graph: the visited check happens BEFORE an id is pushed."""
    descendants: set[str] = set()
    stack = [stage_id]
    while stack:
        upstream = stack.pop()
        for workflow_stage in by_id.values():
            reads = [source.id for source in workflow_stage.inputs]
            if upstream in reads and workflow_stage.id not in descendants:
                descendants.add(workflow_stage.id)
                stack.append(workflow_stage.id)
    return descendants


# ── Precondition: the override must declare an output schema ─────────────────
def _validate_override_declares_output_schema(
    config: EvalConfig, by_id: dict[str, WorkflowStage]
) -> list[str]:
    if config.table is None:
        return []
    override = by_id[config.override_stage]
    if override.output_schema is None:
        return [f"override stage `{override.id}` declares no output schema"]
    return []


# ── Condition 2: every injected table is a valid stand-in ────────────────────
def _validate_eval_dataset_covers_override(
    config: EvalConfig, by_id: dict[str, WorkflowStage]
) -> list[str]:
    if config.table is None:
        return []
    override = by_id[config.override_stage]
    check_output_columns = [expected_output.output_column for expected_output in config.expected_outputs]
    required = TableSchema(columns=get_injected_columns(
        override, by_id[config.target_stage], check_output_columns))
    return _validate_columns_covered(
        required, config.table.table_schema, config.override_stage, "eval-dataset table")


def _validate_reference_overrides_cover_stages(
    config: EvalConfig, by_id: dict[str, WorkflowStage]
) -> list[str]:
    problems: list[str] = []
    for ov in config.reference_overrides:
        stage_output = by_id[ov.stage_id].output_schema
        if stage_output is None:
            problems.append(f"cannot verify reference override `{ov.stage_id}`: "
                            f"stage declares no output schema")
            continue
        problems += _validate_columns_covered(
            stage_output, ov.table.table_schema, ov.stage_id,
            f"reference override `{ov.stage_id}`")
    return problems


def _validate_columns_covered(required: TableSchema, provided: TableSchema,
                           stage_id: str, label: str) -> list[str]:
    return [f"{label}: injected table lacks (or mismatches) column `{col.name}` "
           f"required by stage `{stage_id}`"
           for col in required.subtract(provided, strict=False).columns]


# ── Conditions 3 + 3b: the target's declared output covers the checks ────────
def _validate_target_emits_checked_columns(
    config: EvalConfig, by_id: dict[str, WorkflowStage]
) -> list[str]:
    target = by_id[config.target_stage]
    target_output = target.output_schema
    if target_output is None:
        return [f"cannot verify assertions: target `{target.id}` declares no output schema"]
    problems: list[str] = []
    for expected_output in config.expected_outputs:
        col = target_output.column_for_name(expected_output.output_column)
        if col is None:
            problems.append(f"expected output asserts on `{expected_output.output_column}`, "
                            f"which target `{target.id}` does not emit")
        elif expected_output.metric == ScoringMetric.abs_tol and col.type not in _NUMERIC_TYPES:
            problems.append(f"`{expected_output.output_column}` is `{col.type}` on target "
                            f"`{target.id}` but metric abs_tol needs a numeric")
    return problems


# ── Condition 4: a reference override cannot also be the target ──────────────
def _validate_no_reference_override_on_target(config: EvalConfig) -> list[str]:
    return [f"reference override `{ov.stage_id}` cannot be the target stage"
           for ov in config.reference_overrides if ov.stage_id == config.target_stage]


# ── Structural: the stage list itself must be a valid workflow ───────────────
def _describe_broken_workflow(issues: Sequence[str]) -> list[str]:
    return ["cannot verify the path: the workflow has structural problems: "
           + "; ".join(issues)]


# ── Condition 5: the path must preserve grain (or fall back to a code scorer) ─
def _resolve_grain_settings(
    config: EvalConfig, workflow: Workflow
) -> tuple[EvalRunSettings, list[str]]:
    overrides = [config.override_stage,
                *(ov.stage_id for ov in config.reference_overrides)]
    settings = resolve_eval_run_settings(workflow, overrides, config.target_stage)
    if settings.can_score_declaratively or config.code is not None:
        return settings, []
    return settings, [
        "not scorable row-by-row (blocking stages: "
        + ", ".join(f"`{b}`" for b in settings.blocking_stages)
        + ") and no code scorer is supplied"]
