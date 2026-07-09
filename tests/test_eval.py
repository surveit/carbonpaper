"""Tests for the eval contract (app/models/eval.py + table.py) and the
grain-preservation gate on Stage that governs it (app/models/stage.py)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app import models as m
from app.models import resolve_eval_run_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def S(**kw):
    kw.setdefault("name", kw.get("id", "x"))
    return kw


def _file_input(id_):
    return S(id=id_, type="input_data",
             connector={"kind": "file", "params": {"path": f"{id_}.csv"}})


def _py(id_, inputs, granularity="frame", **kw):
    """granularity 'row' -> python_row_function, else python_frame_function."""
    type_ = "python_row_function" if granularity == "row" else "python_frame_function"
    return S(id=id_, type=type_, inputs=[{"id": i} for i in inputs],
             function={"kind": "inline", "code": "x"}, **kw)


def _ref(path="x.csv", cols=("k",)):
    return {"path": path, "format": "csv",
            "table_schema": {"columns": [{"name": c} for c in cols]}}


# ── is_grain_preserving (fixed by stage type) ────────────────────────────────
def test_python_frame_function_not_grain_preserving():
    assert m.Stage.model_validate(_py("t", ["a"])).is_grain_preserving is False


def test_python_row_function_is_grain_preserving():
    assert m.Stage.model_validate(_py("t", ["a"], granularity="row")).is_grain_preserving is True


def test_python_row_function_rejects_multiple_inputs():
    # a row function maps over one input's rows — two inputs is a join
    with pytest.raises(ValidationError):
        m.Stage.model_validate(S(id="t", type="python_row_function",
                                 inputs=[{"id": "a"}, {"id": "b"}],
                                 function={"kind": "inline", "code": "x"}))


def test_llm_is_grain_preserving():
    s = m.Stage.model_validate(S(id="e", type="llm_transform", inputs=[{"id": "a"}],
                                 llm={"prompt_template": "p"}))
    assert s.is_grain_preserving is True


def test_input_data_is_grain_preserving():
    assert m.Stage.model_validate(_file_input("load")).is_grain_preserving is True


def test_join_and_aggregate_change_grain():
    j = m.Stage.model_validate(S(id="j", type="join", inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"keys": [{"left": "k", "right": "k"}]}))
    agg = m.Stage.model_validate(S(id="agg", type="aggregate", inputs=[{"id": "a"}],
                                   aggregate={"group_by": ["g"],
                                              "aggregations": [{"formula": "sum", "output_column": "t",
                                                                "value_column": "x"}]}))
    assert j.is_grain_preserving is False    # fan-out
    assert agg.is_grain_preserving is False  # fan-in


# ── TableRef (general, schema now required) ──────────────────────────────────
def test_tableref_valid():
    assert m.TableRef.model_validate(_ref()).path == "x.csv"


def test_tableref_schema_required():
    with pytest.raises(ValidationError):
        m.TableRef.model_validate({"path": "x.csv", "format": "csv"})


# ── EvalConfig (merged dataset + eval, one row-aligned table) ────────────────
def _config(**over):
    base = {
        "id": "scoring", "methodology": "lobbymap", "name": "n",
        "override_stage": "evidence_with_benchmarks", "target_stage": "benchmark_scoring",
        "table": _ref(cols=["evidence_id", "benchmark_id", "quote", "expected_score"]),
        "expected": [{"actual": "score", "metric": "abs_tol", "tolerance": 1}],
    }
    base.update(over)
    return base


def test_eval_config_valid():
    c = m.EvalConfig.model_validate(_config(
        reference_overrides=[{"stage_id": "benchmark_library", "table": _ref()}],
        metrics=["mean_absolute_error"]))
    assert c.target_stage == "benchmark_scoring"
    assert c.expected[0].actual == "score"


def test_eval_config_bad_id():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(id="Bad Id"))


def test_eval_config_override_equals_target():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(target_stage="evidence_with_benchmarks"))


def test_eval_config_nonempty_expected():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(expected=[]))


def test_eval_config_duplicate_reference_override():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(
            reference_overrides=[{"stage_id": "a", "table": _ref()},
                                 {"stage_id": "a", "table": _ref()}]))


def test_eval_config_code_scorer():
    c = m.EvalConfig.model_validate(_config(code={"module": "evals.org", "function": "score"}))
    assert c.code.function == "score"


def test_expected_column_abs_tol_needs_tolerance():
    with pytest.raises(ValidationError):
        m.ExpectedColumn.model_validate({"actual": "a", "metric": "abs_tol"})


def test_expected_column_valid_with_no_expected_field():
    c = m.ExpectedColumn.model_validate({"actual": "a"})
    assert c.actual == "a"
    assert not hasattr(c, "expected")


def test_expected_column_rejects_stray_expected_field():
    # extra="forbid" (app/models/schema.py _Base): a leftover `expected` value
    # from an old config is a validation error, not silently-dropped data.
    with pytest.raises(ValidationError):
        m.ExpectedColumn.model_validate({"actual": "a", "expected": "b"})


def test_stage_output_override():
    o = m.StageOutputOverride.model_validate({"stage_id": "benchmark_library", "table": _ref()})
    assert o.stage_id == "benchmark_library"


# ── EvalRun embeds settings ──────────────────────────────────────────────────
def test_eval_run_embeds_settings():
    r = m.EvalRun.model_validate({
        "id": "run-1", "config": "scoring", "methodology": "lobbymap",
        "methodology_version": "abc123", "status": "scored",
        "settings": {"can_score_declaratively": True,
                     "frontier": ["benchmark_scoring"], "blocking_stages": []},
        "metrics": {"mean_absolute_error": 0.33}})
    assert r.settings.can_score_declaratively is True


# ── resolve_eval_run_settings on a synthetic DAG ─────────────────────────────
def _chain():
    """a(input) → b(row) → c(frame) → d(row)."""
    return m.parse_methodology([
        _file_input("a"),
        _py("b", ["a"], granularity="row"),
        _py("c", ["b"], granularity="frame"),
        _py("d", ["c"], granularity="row"),
    ])


def test_blocked_by_frame_on_frontier():
    v = resolve_eval_run_settings(_chain(), overrides=[], target="d")
    assert v.can_score_declaratively is False
    assert v.blocking_stages == ["c"]
    assert set(v.frontier) == {"a", "b", "c", "d"}


def test_override_cuts_above_the_frame_stage():
    v = resolve_eval_run_settings(_chain(), overrides=["c"], target="d")
    assert v.can_score_declaratively is True
    assert v.frontier == ["d"]


def test_scorable_when_tapping_before_the_frame_stage():
    v = resolve_eval_run_settings(_chain(), overrides=[], target="b")
    assert v.can_score_declaratively is True
    assert set(v.frontier) == {"a", "b"}


def test_join_changes_grain_so_not_scorable():
    meth = m.parse_methodology([
        _file_input("j1"), _file_input("j2"),
        S(id="jn", type="join", inputs=[{"id": "j1"}, {"id": "j2"}],
          join={"keys": [{"left": "k", "right": "k"}]}),
    ])
    v = resolve_eval_run_settings(meth, overrides=[], target="jn")
    assert v.can_score_declaratively is False
    assert v.blocking_stages == ["jn"]


# ── EvalConfig.table optional ────────────────────────────────────────────────
def test_eval_config_table_optional():
    # A config with no cases file is valid: expected is still required.
    cfg = m.EvalConfig.model_validate({
        "id": "e1", "methodology": "m", "name": "E1",
        "override_stage": "a", "target_stage": "b",
        "expected": [{"actual": "score", "metric": "exact"}],
    })
    assert cfg.table is None


def test_eval_config_no_key_or_input_columns_fields():
    # `key` and `input_columns` are not part of the contract: alignment is by
    # lineage (computed at run time), and the injected columns are derived
    # from override_stage's output schema, not authored.
    cfg = m.EvalConfig.model_validate({
        "id": "e1", "methodology": "m", "name": "E1",
        "override_stage": "a", "target_stage": "b",
        "expected": [{"actual": "score", "metric": "exact"}],
    })
    assert not hasattr(cfg, "key")
    assert not hasattr(cfg, "input_columns")


def test_eval_config_rejects_stray_key_field():
    # extra="forbid" (app/models/schema.py _Base) means a leftover `key`
    # value from an old config is a validation error, not silently-dropped data.
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate({
            "id": "e1", "methodology": "m", "name": "E1",
            "override_stage": "a", "target_stage": "b",
            "key": ["doc_id"],
            "expected": [{"actual": "score", "metric": "exact"}],
        })


def test_eval_run_has_no_passed_field():
    run = m.EvalRun.model_validate({
        "id": "r1", "config": "e1", "methodology": "m",
        "methodology_version": "v1", "status": "scored",
        "settings": {"can_score_declaratively": True, "frontier": ["b"], "blocking_stages": []},
        "metrics": {"match_rate": 1.0},
    })
    assert not hasattr(run, "passed")


def test_unknown_target_raises():
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(), overrides=[], target="ghost")


def test_unknown_override_raises():
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(), overrides=["ghost"], target="d")


def test_target_in_overrides_raises():
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(), overrides=["d"], target="d")


# ── resolve_eval_run_settings on the real lobbymap DAG ───────────────────────
def _load_lobbymap() -> m.Methodology:
    compiled = REPO_ROOT / "examples" / "lobbymap" / "compiled"
    if not compiled.is_dir():
        pytest.skip("lobbymap example not present")
    stages = []
    for f in sorted(compiled.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        if data:
            stages.append(data)
    return m.parse_methodology(stages)


def test_lobbymap_parses_under_current_contract():
    meth = _load_lobbymap()
    assert any(s.id == "benchmark_scoring" for s in meth.stages)


def test_lobbymap_scoring_is_scorable():
    # tap the scorer LLM, inject the join output above it → frontier is one
    # grain-preserving llm stage
    v = resolve_eval_run_settings(_load_lobbymap(),
                                  overrides=["evidence_with_benchmarks"], target="benchmark_scoring")
    assert v.can_score_declaratively is True
    assert v.frontier == ["benchmark_scoring"]


def test_lobbymap_org_score_not_scorable():
    # the python group-bys (default frame) change grain
    v = resolve_eval_run_settings(_load_lobbymap(), overrides=[], target="org_score")
    assert v.can_score_declaratively is False
    assert {"cell_aggregation", "org_score"} <= set(v.blocking_stages)


def test_lobbymap_frame_node_blocks_in_isolation():
    # override org_score's other inputs (cell_aggregation, tracked_entities) so the
    # frontier is just org_score + the input_data weights table
    v = resolve_eval_run_settings(_load_lobbymap(),
                                  overrides=["cell_aggregation", "tracked_entities"], target="org_score")
    assert v.can_score_declaratively is False
    assert v.blocking_stages == ["org_score"]
