from __future__ import annotations

import pytest

from conftest import queue_added_columns, queue_columns, reads_of
from pydantic import ValidationError

from app import models as m
from app.models.records.eval_config import EvalConfig
from app.models.records.eval_run import EvalRun
from app.evals.run_settings import resolve_eval_run_settings


def S(**kw):
    kw.setdefault("description", kw.get("id", "x"))
    return kw


_K = {"columns": [{"name": "k", "type": "str", "nullable": True}]}
_KV = {"columns": [{"name": "k", "type": "str", "nullable": True},
                    {"name": "v", "type": "str", "nullable": True}]}
_QUEUE_IN = {"columns": _K["columns"] + [{"name": "score", "type": "int", "nullable": True}]}
_QUEUE_OUT = {"columns": _QUEUE_IN["columns"] + queue_added_columns()}


def _file_input(id_, tmp_path, output_schema=_K):
    return S(id=id_, type="input_data",
             signature={"form": "replaces", "produces": output_schema["columns"]},
             connector={"kind": "file", "params": {"path": str(tmp_path / f"{id_}.csv")}})


def _py(id_, inputs, granularity="frame", schema=_K, **kw):
    type_ = "python_row_function" if granularity == "row" else "python_frame_function"
    signature = ({"form": "extends"} if granularity == "row"
                 else {"form": "replaces", "produces": schema["columns"]})
    return S(id=id_, type=type_, inputs=[{"id": i} for i in inputs],
             function={"kind": "inline", "code": "def transform(row): return row"},
             signature=signature, **kw)


def _ref(path="x.csv", cols=("k",)):
    return {"path": path, "format": "csv",
            "table_schema": {"columns": [{"name": c, "type": "str", "nullable": True} for c in cols]}}


# ── is_grain_and_order_preserving (fixed by stage type) ────────────────────────────────
def test_python_frame_function_not_grain_preserving():
    assert m.parse_stage(_py("t", ["a"])).is_grain_and_order_preserving is False


def test_python_row_function_is_grain_and_order_preserving():
    assert m.parse_stage(_py("t", ["a"], granularity="row")).is_grain_and_order_preserving is True


def test_python_row_function_rejects_multiple_inputs():
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="t", type="python_row_function",
                                 inputs=[{"id": "a"}, {"id": "b"}],
                                 function={"kind": "inline", "code": "def transform(row): return row"},
                                 signature={
                                     "form": "extends",
                                     "reads": [{"input": "a", "columns": _K["columns"]}],
                                 }))


def test_llm_is_grain_and_order_preserving():
    s = m.parse_stage(S(
        id="e", type="llm_transform",
        inputs=[{"id": "a"}],
        signature={"form": "extends", "adds": [{"name": "out", "type": "str", "nullable": True}]},
        llm={"prompt_template": "p"}))
    assert s.is_grain_and_order_preserving is True


def test_input_data_is_grain_and_order_preserving(tmp_path):
    assert m.parse_stage(_file_input("load", tmp_path)).is_grain_and_order_preserving is True


def test_human_review_queue_is_grain_and_order_preserving():
    # The queue emits every row whatever the verdict, so it never drops the rejected ones.
    s = m.parse_stage(S(id="rev", type="human_review_queue",
                        inputs=[{"id": "a"}],
                        queue=queue_columns(), signature={
                            "form": "extends",
                            "reads": reads_of("a", _QUEUE_IN["columns"]),
                            "adds": [
                                {"name": "human_score", "type": "int", "nullable": True},
                                {"name": "decision", "type": "str", "nullable": True},
                                {"name": "reviewer_id", "type": "str", "nullable": True},
                                {"name": "reviewed_at", "type": "str", "nullable": True},
                                {
                                    "name": "review_notes",
                                    "type": "str",
                                    "nullable": True,
                                },
                            ],
                        }))
    assert s.is_grain_and_order_preserving is True


def test_report_not_grain_and_order_preserving():
    s = m.parse_stage(S(id="pub", type="report",
                                 inputs=[{"id": "a"}], report={},
                                 signature={"form": "replaces"},
                                 function={"kind": "inline", "code": "def transform(row): return row"}))
    assert s.is_grain_and_order_preserving is False


def test_joins_and_aggregate_change_grain():
    # enrich is m:1 yet False: preservation is earned by the row driver, never by the operation.
    j = m.parse_stage(S(id="j", type="enrich",
                                 inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"keys": [{"left": "k", "right": "k"}], "enrich_with": {"v": "v"}},
                                 signature={
                                     "form": "extends",
                                     "reads": [
                                         {"input": "a", "columns": _K["columns"]},
                                         {"input": "b", "columns": _K["columns"]},
                                     ],
                                     "adds": [{"name": "v", "type": "str", "nullable": True}],
                                 }))
    x = m.parse_stage(S(id="x", type="expand",
                                 inputs=[{"id": "a"}, {"id": "b"}],
                                 join={"keys": [{"left": "k", "right": "k"}], "enrich_with": {"v": "v"}},
                                 signature={
                                     "form": "extends",
                                     "reads": [
                                         {"input": "a", "columns": _K["columns"]},
                                         {"input": "b", "columns": _K["columns"]},
                                     ],
                                     "adds": [{"name": "v", "type": "str", "nullable": True}],
                                 }))
    assert x.is_grain_and_order_preserving is False
    agg_in = {"columns": [{"name": "g", "type": "str", "nullable": True}, {"name": "x", "type": "int", "nullable": True}]}
    agg = m.parse_stage(S(id="agg", type="aggregate",
                                   inputs=[{"id": "a"}],
                                   aggregate={"group_by": ["g"],
                                              "aggregations": [{"formula": "sum", "output_column": "t",
                                                                "value_column": "x"}]},
                                   signature={
                                       "form": "replaces",
                                       "reads": [{"input": "a", "columns": agg_in["columns"]}],
                                       "produces": [{"name": "g", "type": "str", "nullable": True},
                                                    {"name": "t", "type": "int", "nullable": True}]}))
    assert j.is_grain_and_order_preserving is False    # fan-out
    assert agg.is_grain_and_order_preserving is False  # fan-in


# ── TableRef (general, schema now required) ──────────────────────────────────
def test_tableref_valid():
    assert m.TableRef.model_validate(_ref()).path == "x.csv"


def test_tableref_schema_required():
    with pytest.raises(ValidationError):
        m.TableRef.model_validate({"path": "x.csv", "format": "csv"})


# ── EvalConfig (defined by its checks; eval-dataset table is optional data) ──
def _config(**over):
    base = {
        "eval_id": "scoring", "project": "lobbymap", "name": "n",
        "override_stage": "evidence_with_benchmarks", "target_stage": "benchmark_scoring",
        "table": _ref(cols=["evidence_id", "benchmark_id", "quote", "expected_score"]),
        "expected_outputs": [{"output_column": "score", "metric": "abs_tol", "tolerance": 1}],
    }
    base.update(over)
    return base


def test_eval_config_valid():
    c = EvalConfig.model_validate(_config(
        reference_overrides=[{"stage_id": "benchmark_library", "table": _ref()}],
        metrics=["mean_absolute_error"]))
    assert c.target_stage == "benchmark_scoring"
    assert c.expected_outputs[0].output_column == "score"


def test_eval_config_bad_id():
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(_config(eval_id="Bad Id"))


def test_eval_config_override_equals_target():
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(_config(target_stage="evidence_with_benchmarks"))


def test_eval_config_nonempty_expected_outputs():
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(_config(expected_outputs=[]))


def test_eval_config_table_optional():
    cfg = EvalConfig.model_validate({
        "eval_id": "e1", "project": "lobbymap", "name": "E1",
        "override_stage": "a", "target_stage": "b",
        "expected_outputs": [{"output_column": "score", "metric": "exact"}],
    })
    assert cfg.table is None


def test_eval_config_no_key_or_input_columns_fields():
    cfg = EvalConfig.model_validate({
        "eval_id": "e1", "project": "lobbymap", "name": "E1",
        "override_stage": "a", "target_stage": "b",
        "expected_outputs": [{"output_column": "score", "metric": "exact"}],
    })
    assert not hasattr(cfg, "key")
    assert not hasattr(cfg, "input_columns")


def test_eval_config_rejects_stray_key_field():
    with pytest.raises(ValidationError):
        EvalConfig.model_validate({
            "eval_id": "e1", "project": "lobbymap", "name": "E1",
            "override_stage": "a", "target_stage": "b",
            "key": ["doc_id"],
            "expected_outputs": [{"output_column": "score", "metric": "exact"}],
        })


def test_eval_config_duplicate_reference_override():
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(_config(
            reference_overrides=[{"stage_id": "a", "table": _ref()},
                                 {"stage_id": "a", "table": _ref()}]))


def test_eval_config_code_scorer():
    c = EvalConfig.model_validate(_config(code={"module": "evals.org", "function": "score"}))
    assert c.code.function == "score"


def test_expected_output_abs_tol_needs_tolerance():
    with pytest.raises(ValidationError):
        m.ExpectedOutput.model_validate({"output_column": "a", "metric": "abs_tol"})


def test_expected_output_valid_with_no_expected_field():
    c = m.ExpectedOutput.model_validate({"output_column": "a"})
    assert c.output_column == "a"
    assert not hasattr(c, "expected")


def test_expected_output_rejects_stray_expected_field():
    with pytest.raises(ValidationError):
        m.ExpectedOutput.model_validate({"output_column": "a", "expected": "b"})


def test_stage_output_override():
    o = m.StageOutputOverride.model_validate({"stage_id": "benchmark_library", "table": _ref()})
    assert o.stage_id == "benchmark_library"


# ── EvalRun embeds settings (no overall pass/fail) ────────────────────────────
def test_eval_run_embeds_settings():
    r = EvalRun.model_validate({
        "run_id": "run-1", "config": "scoring", "project": "lobbymap",
        "workflow_version": "abc123", "status": "scored",
        "settings": {"can_score_declaratively": True,
                     "frontier": ["benchmark_scoring"], "blocking_stages": []},
        "metrics": {"mean_absolute_error": 0.33}})
    assert r.settings.can_score_declaratively is True


def test_eval_run_has_no_passed_field():
    run = EvalRun.model_validate({
        "run_id": "r1", "config": "e1", "project": "lobbymap",
        "workflow_version": "v1", "status": "scored",
        "settings": {"can_score_declaratively": True, "frontier": ["b"], "blocking_stages": []},
        "metrics": {"match_rate": 1.0},
    })
    assert not hasattr(run, "passed")


# ── resolve_eval_run_settings on a synthetic workflow ─────────────────────────────
def _chain(tmp_path):
    return m.parse_workflow([
        _file_input("a", tmp_path),
        _py("b", ["a"], granularity="row"),
        _py("c", ["b"], granularity="frame"),
        _py("d", ["c"], granularity="row"),
    ])


def test_blocked_by_frame_on_frontier(tmp_path):
    v = resolve_eval_run_settings(_chain(tmp_path), overrides=[], target="d")
    assert v.can_score_declaratively is False
    assert v.blocking_stages == ["c"]
    assert set(v.frontier) == {"a", "b", "c", "d"}


def test_override_cuts_above_the_frame_stage(tmp_path):
    v = resolve_eval_run_settings(_chain(tmp_path), overrides=["c"], target="d")
    assert v.can_score_declaratively is True
    assert v.frontier == ["d"]


def test_scorable_when_tapping_before_the_frame_stage(tmp_path):
    v = resolve_eval_run_settings(_chain(tmp_path), overrides=[], target="b")
    assert v.can_score_declaratively is True
    assert set(v.frontier) == {"a", "b"}


def test_expand_changes_grain_so_not_scorable(tmp_path):
    meth = m.parse_workflow([
        _file_input("j1", tmp_path), _file_input("j2", tmp_path, output_schema=_KV),
        S(id="jn", type="expand", inputs=[{"id": "j1"}, {"id": "j2"}],
          join={"keys": [{"left": "k", "right": "k"}], "enrich_with": {"v": "v"}}, signature={
              "form": "extends",
              "reads": [
                  {"input": "j1", "columns": _K["columns"]},
                  {"input": "j2", "columns": _K["columns"]},
              ],
              "adds": [{"name": "v", "type": "str", "nullable": True}],
          }),
    ])
    v = resolve_eval_run_settings(meth, overrides=[], target="jn")
    assert v.can_score_declaratively is False
    assert v.blocking_stages == ["jn"]


def test_unknown_target_raises(tmp_path):
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(tmp_path), overrides=[], target="ghost")


def test_unknown_override_raises(tmp_path):
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(tmp_path), overrides=["ghost"], target="d")


def test_target_in_overrides_raises(tmp_path):
    with pytest.raises(ValueError):
        resolve_eval_run_settings(_chain(tmp_path), overrides=["d"], target="d")
