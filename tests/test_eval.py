"""Tests for the eval contract (app/models/eval.py + table.py) and the
grain-preservation gate on Stage that governs it (app/models/stage.py)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m
from app.models import resolve_eval_run_settings


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
        "id": "scoring", "project": "lobbymap", "name": "n",
        "override_stage": "evidence_with_benchmarks", "target_stage": "benchmark_scoring",
        "table": _ref(cols=["evidence_id", "benchmark_id", "quote", "expected_score"]),
        "key": ["evidence_id", "benchmark_id"],
        "input_columns": ["quote"],
        "expected": [{"actual": "score", "expected": "expected_score",
                      "metric": "abs_tol", "tolerance": 1}],
    }
    base.update(over)
    return base


def test_eval_config_valid():
    c = m.EvalConfig.model_validate(_config(
        reference_overrides=[{"stage_id": "benchmark_library", "table": _ref()}],
        metrics=["mean_absolute_error"]))
    assert c.target_stage == "benchmark_scoring"
    assert c.expected[0].expected == "expected_score"


def test_eval_config_bad_id():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(id="Bad Id"))


def test_eval_config_override_equals_target():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(target_stage="evidence_with_benchmarks"))


@pytest.mark.parametrize("field", ["key", "input_columns", "expected"])
def test_eval_config_nonempty(field):
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(**{field: []}))


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
        m.ExpectedColumn.model_validate({"actual": "a", "expected": "b", "metric": "abs_tol"})


def test_stage_output_override():
    o = m.StageOutputOverride.model_validate({"stage_id": "benchmark_library", "table": _ref()})
    assert o.stage_id == "benchmark_library"


# ── EvalRun embeds settings ──────────────────────────────────────────────────
def test_eval_run_embeds_settings_and_passed():
    r = m.EvalRun.model_validate({
        "id": "run-1", "config": "scoring", "project": "lobbymap",
        "workflow_version": "abc123", "status": "scored",
        "settings": {"can_score_declaratively": True,
                     "frontier": ["benchmark_scoring"], "blocking_stages": []},
        "passed": True, "metrics": {"mean_absolute_error": 0.33}})
    assert r.settings.can_score_declaratively is True
    assert r.passed is True


# ── resolve_eval_run_settings on a synthetic workflow ─────────────────────────────
def _chain():
    """a(input) → b(row) → c(frame) → d(row)."""
    return m.parse_workflow([
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
    meth = m.parse_workflow([
        _file_input("j1"), _file_input("j2"),
        S(id="jn", type="join", inputs=[{"id": "j1"}, {"id": "j2"}],
          join={"keys": [{"left": "k", "right": "k"}]}),
    ])
    v = resolve_eval_run_settings(meth, overrides=[], target="jn")
    assert v.can_score_declaratively is False
    assert v.blocking_stages == ["jn"]


def test_unknown_target_raises():
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(), overrides=[], target="ghost")


def test_unknown_override_raises():
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(), overrides=["ghost"], target="d")


def test_target_in_overrides_raises():
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(), overrides=["d"], target="d")
