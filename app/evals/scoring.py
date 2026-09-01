"""Score a workflow target's output against an eval dataset's expected columns.
Row alignment is by POSITION: target row i came from eval-dataset row i, which
holds only for paths compatibility judged grain-and-order-preserving. A length
mismatch means a stage broke that claim and raises rather than aligning a
mismatched pair; a reorder that kept the count can't be detected post-hoc.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.core.errors import EvalGrainViolationError
from app.models import ScoringMetric, WorkflowStage
from app.models.records.eval_config import EvalConfig
from app.evals.dataset_columns import (
    deconflict_column_names,
    get_output_columns_from_stage,
)


@dataclass
class ScoreResult:
    metrics: dict[str, Any]
    per_row: pd.DataFrame


def score_expected_outputs(
    config: EvalConfig, override: WorkflowStage, target: WorkflowStage,
    dataset_df: pd.DataFrame, target_df: pd.DataFrame,
) -> ScoreResult:
    if len(dataset_df) != len(target_df):
        raise EvalGrainViolationError(
            f"eval dataset has {len(dataset_df)} row(s) but the target produced "
            f"{len(target_df)} — the override→target path did not preserve grain, "
            "so rows can't be aligned to score")
    checks = _resolve_checks(config, override, target)
    per_row = _build_per_row_results(checks, dataset_df, target_df)
    return ScoreResult(metrics=_roll_up_metrics(checks, per_row), per_row=per_row)


@dataclass
class _Check:
    expected_column: str
    target_column: str
    metric: str
    tolerance: float | None


def _resolve_checks(
    config: EvalConfig, override: WorkflowStage, target: WorkflowStage
) -> list[_Check]:
    target_by_name = {c.name: c for c in get_output_columns_from_stage(target)}
    expected_source = [target_by_name[c.output_column] for c in config.expected_outputs]
    _, expected_columns = deconflict_column_names(
        get_output_columns_from_stage(override), expected_source)
    return [
        _Check(expected_column=expected_columns[i].name,
               target_column=check.output_column,
               metric=check.metric, tolerance=check.tolerance)
        for i, check in enumerate(config.expected_outputs)
    ]


def _build_per_row_results(
    checks: list[_Check], dataset_df: pd.DataFrame, target_df: pd.DataFrame,
) -> pd.DataFrame:
    columns: dict[str, list[Any]] = {}
    match_flags: list[list[bool]] = []
    for check in checks:
        expected = list(dataset_df[check.expected_column])
        actual = list(target_df[check.target_column])
        matches = [_value_matches(e, a, check.metric, check.tolerance)
                   for e, a in zip(expected, actual)]
        columns[f"{check.target_column}__expected"] = expected
        columns[f"{check.target_column}__actual"] = actual
        columns[f"{check.target_column}__match"] = matches
        match_flags.append(matches)
    row_passed = [all(row) for row in zip(*match_flags)] if match_flags else []
    return pd.DataFrame({**columns, "row_passed": row_passed})


def _roll_up_metrics(checks: list[_Check], per_row: pd.DataFrame) -> dict[str, Any]:
    n = len(per_row)
    metrics: dict[str, Any] = {
        "rows_scored": n,
        "rows_passed": int(per_row["row_passed"].sum()) if n else 0,
        "accuracy": float(per_row["row_passed"].mean()) if n else 0.0,
    }
    for check in checks:
        col = f"{check.target_column}__match"
        metrics[f"accuracy.{check.target_column}"] = (
            float(per_row[col].mean()) if n else 0.0)
    return metrics


def _value_matches(expected: Any, actual: Any, metric: str, tolerance: float | None) -> bool:
    expected_is_null, actual_is_null = pd.isna(expected), pd.isna(actual)
    if expected_is_null or actual_is_null:
        # a null expectation asserts the row has no value, which only a null answer meets
        return expected_is_null and actual_is_null
    if metric == ScoringMetric.exact:
        return bool(expected == actual)
    if metric == ScoringMetric.abs_tol:
        assert tolerance is not None  # EvalConfig validates abs_tol carries one
        return abs(float(actual) - float(expected)) <= tolerance
    if metric == ScoringMetric.sign:
        return _sign(float(actual)) == _sign(float(expected))
    raise ValueError(f"unknown metric {metric!r}")


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)
