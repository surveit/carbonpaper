"""Score a workflow target's output against an eval dataset's expected columns.

The eval dataset holds, per row, the override stage's input columns (injected to
produce the output) alongside one expected-output column per check. This module
compares each check's expected column to the matching column the target actually
produced, row by row, and rolls the per-row matches up into metrics.

Row alignment is by POSITION: target row i was produced from eval-dataset row i.
That holds because this only scores paths compatibility judged grain-preserving, and
`Stage.is_grain_and_order_preserving` is defined as 1:1 AND order-preserving (see its docstring
— the guarantee is declared where a stage claims it, not assumed here). The one
observable consequence — row count — is still checked: a length mismatch means a stage
broke the grain claim, and `score_expected_outputs` raises rather than align a
mismatched pair (which would report a fabricated result). A reorder that kept the count
can't be detected post-hoc, which is exactly why order-preservation is part of the
is_grain_and_order_preserving contract rather than a hope at this call site.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.core.errors import EvalGrainViolationError
from app.models import EvalConfig, ScoringMetric, Stage
from app.evals.dataset_columns import (
    deconflict_column_names,
    get_output_columns_from_stage,
)


@dataclass
class ScoreResult:
    """The outcome of scoring one run: rollup `metrics` and the per-row result
    table (one row per eval-dataset row, with each check's expected/actual/match
    and whether the whole row passed)."""
    metrics: dict[str, Any]
    per_row: pd.DataFrame


def score_expected_outputs(
    config: EvalConfig, override: Stage, target: Stage,
    dataset_df: pd.DataFrame, target_df: pd.DataFrame,
) -> ScoreResult:
    """Compare each check's expected-output column (in `dataset_df`) to the column
    the target actually emitted (in `target_df`), aligned by row position.

    Raises EvalGrainViolationError if the two frames differ in length — the
    grain-preserving precondition positional alignment depends on did not hold."""
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
    """One resolved check: the dataset column holding the expected value, the
    target column that produced the actual value, and how to compare them."""
    expected_column: str
    target_column: str
    metric: str
    tolerance: float | None


def _resolve_checks(config: EvalConfig, override: Stage, target: Stage) -> list[_Check]:
    """Pair each check's target column with the (possibly deconflicted) dataset
    column that carries its expected value. `deconflict_column_names` renames
    every expected column the same way the eval-dataset schema was built, so the
    names line up with the columns actually in `dataset_df`."""
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
    """One row per eval-dataset row: each check's expected value, actual value, and
    match flag, plus `row_passed` (all checks matched)."""
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
    """Accuracy over rows (a row counts only if all its checks matched) plus
    per-check accuracy, so a reviewer sees both the headline and which check drags
    it down."""
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
    """Does `actual` match `expected` under `metric`? exact = equality; abs_tol =
    within `tolerance`; sign = same sign. A null on either side is never a match
    (an unverifiable pair is not a pass)."""
    if pd.isna(expected) or pd.isna(actual):
        return False
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
