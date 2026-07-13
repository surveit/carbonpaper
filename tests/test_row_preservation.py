"""Row-preservation is a runtime-declared, runtime-ENFORCED contract (issue #87).

Three layers, one property:
  - `Stage.is_row_preserving` declares, from `type` alone, the stages that are
    1:1 by construction (input_data / python_row_function / llm_transform).
  - `runtime.validation.check_row_preservation` enforces it: a stage that claims
    row-preservation but emits a different row count than its input fails loudly.
  - The runner persists the declared property into every manifest stage record,
    so the show-your-work lineage tracer can read a property the runtime
    guarantees instead of inferring it from a hardcoded stage-type set.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.models import Stage
from app.runtime.runner import execute_run
from app.runtime.stages import llm_transform as lt
from app.runtime.validation import RowPreservationError, check_row_preservation
from app.services.versioning import create_version


# ── The declared property (derived purely from stage type) ───────────────────

def _minimal_stage(stage_type: str) -> Stage:
    """A minimal VALID stage of each type — enough for the type-derived
    `is_row_preserving` property (row-preservation depends on `type` only)."""
    handles: dict[str, dict] = {
        "input_data": {
            "connector": {"kind": "file",
                          "params": {"path": "data/x.csv", "format": "csv"}},
        },
        "python_row_function": {
            "inputs": [{"id": "up"}],
            "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
        },
        "python_frame_function": {
            "inputs": [{"id": "up"}],
            "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
        },
        "llm_transform": {
            "inputs": [{"id": "up", "schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
                "primary_key": ["id"]}}],
            "output_schema": {
                "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                            {"name": "score", "type": "int"}],
                "primary_key": ["id"]},
            "llm": {"prompt_template": "Rate: {text}"},
        },
        "join": {
            "inputs": [{"id": "a"}, {"id": "b"}],
            "join": {"type": "inner", "keys": [{"left": "id", "right": "id"}]},
        },
        "aggregate": {
            "inputs": [{"id": "up"}],
            "aggregate": {"group_by": ["g"],
                          "aggregations": [{"output_column": "n", "formula": "count"}]},
        },
        "human_review_queue": {
            "inputs": [{"id": "up"}],
            "queue": {},
        },
        "publish": {
            "inputs": [{"id": "up"}],
            "publish": {"format": "json"},
            "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
        },
    }
    return Stage.model_validate(
        {"id": f"s_{stage_type}", "name": stage_type, "type": stage_type,
         **handles[stage_type]}
    )


@pytest.mark.parametrize("stage_type", ["input_data", "python_row_function", "llm_transform"])
def test_row_preserving_types_declare_true(stage_type):
    assert _minimal_stage(stage_type).is_row_preserving is True


@pytest.mark.parametrize(
    "stage_type",
    ["python_frame_function", "join", "aggregate", "human_review_queue", "publish"],
)
def test_reshaping_and_terminal_types_declare_false(stage_type):
    # Narrower than is_grain_and_order_preserving: human_review_queue (may DROP
    # rows via its filter) and publish (terminal sink) are grain-and-order-
    # preserving but NOT positionally row-preserving.
    assert _minimal_stage(stage_type).is_row_preserving is False


def test_row_preserving_is_a_strict_subset_of_grain_and_order_preserving():
    for stage_type in ["input_data", "python_row_function", "llm_transform"]:
        s = _minimal_stage(stage_type)
        assert s.is_row_preserving and s.is_grain_and_order_preserving
    for stage_type in ["human_review_queue", "publish"]:
        s = _minimal_stage(stage_type)
        assert s.is_grain_and_order_preserving and not s.is_row_preserving


# ── The enforcement helper (unit) ────────────────────────────────────────────

def test_check_passes_when_counts_match():
    stage = _minimal_stage("python_row_function")
    src = pd.DataFrame({"a": [1, 2, 3]})
    out = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    check_row_preservation(stage, {"up": src}, out)  # no raise


def test_check_raises_when_row_preserving_stage_fans_out():
    stage = _minimal_stage("python_row_function")
    src = pd.DataFrame({"a": [1, 2]})
    fanned = pd.DataFrame({"a": [1, 1, 2, 2]})       # 2 in → 4 out
    with pytest.raises(RowPreservationError) as exc:
        check_row_preservation(stage, {"up": src}, fanned)
    assert "4 row(s) from 2 input row(s)" in str(exc.value)
    assert stage.id in str(exc.value)


def test_check_ignores_non_row_preserving_stage():
    # A frame function may legitimately reshape — never checked.
    stage = _minimal_stage("python_frame_function")
    src = pd.DataFrame({"a": [1, 2, 3]})
    reshaped = pd.DataFrame({"a": [1]})              # dedup/aggregate-style fan-in
    check_row_preservation(stage, {"up": src}, reshaped)  # no raise


def test_check_is_noop_for_input_data_source():
    # input_data originates rows (no input) — nothing to compare against.
    stage = _minimal_stage("input_data")
    out = pd.DataFrame({"a": [1, 2, 3]})
    check_row_preservation(stage, {}, out)  # no raise


# ── The positional key-equivalence backstop (unit) ───────────────────────────
# Redundant with the trusted-by-construction order: when the input declares a
# primary key that survives into the output, the key must line up row-for-row.
# Catches a reorder/re-key bug that leaves the row COUNT intact.

def test_check_raises_when_declared_key_is_misaligned():
    # llm_transform declares primary_key ["id"] on its input; counts match but
    # the output's key is in a different row order than the input's.
    stage = _minimal_stage("llm_transform")
    src = pd.DataFrame({"id": ["a", "b"], "text": ["x", "y"]})
    swapped = pd.DataFrame({"id": ["b", "a"], "text": ["y", "x"], "score": [1, 2]})
    with pytest.raises(RowPreservationError) as exc:
        check_row_preservation(stage, {"up": src}, swapped)
    assert "['id']" in str(exc.value)
    assert stage.id in str(exc.value)


def test_check_passes_when_declared_key_lines_up():
    stage = _minimal_stage("llm_transform")
    src = pd.DataFrame({"id": ["a", "b"], "text": ["x", "y"]})
    aligned = pd.DataFrame({"id": ["a", "b"], "text": ["x", "y"], "score": [1, 2]})
    check_row_preservation(stage, {"up": src}, aligned)  # no raise


def test_check_skips_key_equivalence_when_no_key_declared():
    # python_row_function's input declares no primary_key → stay on the trusted
    # order and never compare keys, even if values differ row-for-row.
    stage = _minimal_stage("python_row_function")
    src = pd.DataFrame({"a": [1, 2]})
    reordered = pd.DataFrame({"a": [2, 1]})
    check_row_preservation(stage, {"up": src}, reordered)  # no raise


def test_check_skips_key_equivalence_when_key_absent_from_output():
    # Key declared on the input but the output no longer carries it → nothing to
    # compare; a missing declared column is already an output-schema validation
    # error, not this check's job to re-flag.
    stage = _minimal_stage("llm_transform")
    src = pd.DataFrame({"id": ["a", "b"], "text": ["x", "y"]})
    no_key = pd.DataFrame({"text": ["y", "x"], "score": [1, 2]})
    check_row_preservation(stage, {"up": src}, no_key)  # no raise


def test_check_tolerates_benign_key_dtype_change():
    # int key in, float key out, same values in the same order → aligned. The
    # backstop guards against reordering, not a benign dtype promotion.
    stage = _minimal_stage("llm_transform")
    src = pd.DataFrame({"id": [1, 2], "text": ["x", "y"]})
    promoted = pd.DataFrame({"id": [1.0, 2.0], "text": ["x", "y"], "score": [1, 2]})
    check_row_preservation(stage, {"up": src}, promoted)  # no raise


# ── End-to-end through the runner (fails loudly, persists the property) ───────

def _llm_project(root):
    """input_data (2 distinct rows) → llm_transform `score` (declared 1:1)."""
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"id": ["r1", "r2"], "text": ["alpha", "beta"]}) \
        .to_csv(root / "data" / "items.csv", index=False)
    load = {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": "data/items.csv", "format": "csv"}},
    }
    score = {
        "id": "score", "name": "Score", "type": "llm_transform",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"}],
            "primary_key": ["id"]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "text", "type": "str"},
                        {"name": "score", "type": "int"}],
            "primary_key": ["id"]},
        "llm": {"prompt_template": "Rate: {text}"},
    }
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_score.json").write_text(json.dumps(score), encoding="utf-8")


def test_declared_row_preserving_stage_traces_fine_and_persists_property(tmp_path, monkeypatch):
    _llm_project(tmp_path)
    create_version(tmp_path, message="seed", reviewer="test")
    # 1:1: one reply dict per input row.
    monkeypatch.setattr(lt, "call_llm_batch",
                        lambda stage_id, llm, rows, **kw: [{"score": 1} for _ in rows])

    manifest = execute_run(tmp_path, repo_root=tmp_path)

    records = {r["stage_id"]: r for r in manifest["stages"]}
    assert records["score"]["status"] == "ok"
    assert records["score"]["rows"] == 2                     # stayed 1:1

    # The declared property is persisted to the manifest for every stage, so the
    # tracer can read it without the compiled Stage object.
    assert records["load"]["is_row_preserving"] is True      # input_data
    assert records["score"]["is_row_preserving"] is True     # llm_transform

    on_disk = json.loads(
        (tmp_path / "runs" / manifest["run_id"] / "manifest.json").read_text("utf-8"))
    assert {r["stage_id"]: r["is_row_preserving"] for r in on_disk["stages"]} == {
        "load": True, "score": True}


def test_row_preserving_stage_that_fans_out_fails_loudly(tmp_path, monkeypatch):
    _llm_project(tmp_path)
    create_version(tmp_path, message="seed", reviewer="test")
    # A stage DECLARED 1:1 (llm_transform) that fans out at runtime: each input
    # row yields a LIST of two replies. This must fail loudly, not silently
    # emit 4 rows the tracer would mis-map by position.
    monkeypatch.setattr(
        lt, "call_llm_batch",
        lambda stage_id, llm, rows, **kw: [[{"score": 1}, {"score": 2}] for _ in rows])

    manifest = execute_run(tmp_path, repo_root=tmp_path)

    records = {r["stage_id"]: r for r in manifest["stages"]}
    assert records["load"]["status"] == "ok"
    assert records["score"]["status"] == "error"            # loud failure
    err = records["score"]["error"]
    assert err["type"] == "RowPreservationError"
    assert "4 row(s) from 2 input row(s)" in err["message"]
    assert manifest["status"] == "errors"

    # No misleading output persisted for the offending stage.
    assert not (tmp_path / "runs" / manifest["run_id"] / "outputs" / "score.parquet").exists()
