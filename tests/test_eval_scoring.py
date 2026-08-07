"""Unit tests for app/evals/scoring.py: metric comparisons, the
positional grain guard, and that a checked column whose name clashes with an
override column is read from the deconflicted `output.<name>` dataset column."""
from __future__ import annotations

import pandas as pd
import pytest

from app import models as m
from app.core.errors import EvalGrainViolationError
from app.models import EvalConfig, ExpectedOutput
from app.evals.scoring import score_expected_outputs


def _stage(id_, output_cols, tmp_path):
    """A minimal input_data stage whose signature produces `output_cols` — enough
    for scoring, which reads only stage ids and resolved output schemas."""
    return m.parse_stage({
        "id": id_, "type": "input_data", "name": id_,
        "connector": {"kind": "file", "params": {"path": str(tmp_path / f"{id_}.csv")}},
        "signature": {"form": "replaces", "produces": output_cols},
    })


def _config(checks):
    return EvalConfig(
        id="e", project="p", name="e", override_stage="ov", target_stage="tg",
        expected_outputs=checks)


def test_exact_metric_counts_matching_rows(tmp_path):
    override = _stage("ov", [{"name": "doc_id", "type": "str", "nullable": True}], tmp_path)
    target = _stage("tg", [{"name": "doc_id", "type": "str", "nullable": True}, {"name": "label", "type": "str", "nullable": True}], tmp_path)
    config = _config([ExpectedOutput(output_column="label", metric="exact")])
    dataset = pd.DataFrame({"doc_id": ["a", "b", "c"], "label": ["x", "y", "z"]})
    target_df = pd.DataFrame({"doc_id": ["a", "b", "c"], "label": ["x", "WRONG", "z"]})

    result = score_expected_outputs(config, override, target, dataset, target_df)
    assert result.metrics["accuracy"] == pytest.approx(2 / 3)
    assert list(result.per_row["row_passed"]) == [True, False, True]


def test_abs_tol_metric_uses_tolerance(tmp_path):
    override = _stage("ov", [{"name": "k", "type": "str", "nullable": True}], tmp_path)
    target = _stage("tg", [{"name": "k", "type": "str", "nullable": True}, {"name": "amt", "type": "float", "nullable": True}], tmp_path)
    config = _config([ExpectedOutput(output_column="amt", metric="abs_tol", tolerance=0.5)])
    dataset = pd.DataFrame({"k": ["a", "b"], "amt": [10.0, 20.0]})
    target_df = pd.DataFrame({"k": ["a", "b"], "amt": [10.4, 21.0]})  # within, then outside

    result = score_expected_outputs(config, override, target, dataset, target_df)
    assert list(result.per_row["row_passed"]) == [True, False]


def test_sign_metric_compares_sign_only(tmp_path):
    override = _stage("ov", [{"name": "k", "type": "str", "nullable": True}], tmp_path)
    target = _stage("tg", [{"name": "k", "type": "str", "nullable": True}, {"name": "delta", "type": "float", "nullable": True}], tmp_path)
    config = _config([ExpectedOutput(output_column="delta", metric="sign")])
    dataset = pd.DataFrame({"k": ["a", "b"], "delta": [5.0, -5.0]})
    target_df = pd.DataFrame({"k": ["a", "b"], "delta": [99.0, 3.0]})  # same sign, then not

    result = score_expected_outputs(config, override, target, dataset, target_df)
    assert list(result.per_row["row_passed"]) == [True, False]


def test_row_count_mismatch_raises_grain_violation(tmp_path):
    override = _stage("ov", [{"name": "k", "type": "str", "nullable": True}], tmp_path)
    target = _stage("tg", [{"name": "k", "type": "str", "nullable": True}, {"name": "label", "type": "str", "nullable": True}], tmp_path)
    config = _config([ExpectedOutput(output_column="label", metric="exact")])
    dataset = pd.DataFrame({"k": ["a", "b", "c"], "label": ["x", "y", "z"]})
    target_df = pd.DataFrame({"k": ["a", "b"], "label": ["x", "y"]})  # a row dropped

    with pytest.raises(EvalGrainViolationError, match="preserve grain"):
        score_expected_outputs(config, override, target, dataset, target_df)


def test_checked_column_clashing_with_override_is_read_from_output_prefixed_column(tmp_path):
    """When a checked target column shares a name with an override output column,
    the expected value lives in the deconflicted `output.<name>` dataset column —
    the scorer must compare THAT to the target's own `<name>`, not the injected
    `override.<name>`."""
    override = _stage("ov", [{"name": "label", "type": "str", "nullable": True}], tmp_path)   # override emits `label`
    target = _stage("tg", [{"name": "label", "type": "str", "nullable": True}], tmp_path)     # target also emits `label`
    config = _config([ExpectedOutput(output_column="label", metric="exact")])
    dataset = pd.DataFrame({"override.label": ["in1", "in2"], "output.label": ["x", "y"]})
    target_df = pd.DataFrame({"label": ["x", "WRONG"]})

    result = score_expected_outputs(config, override, target, dataset, target_df)
    assert list(result.per_row["label__expected"]) == ["x", "y"]   # from output.label
    assert list(result.per_row["row_passed"]) == [True, False]
