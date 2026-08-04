from __future__ import annotations

import pytest

from conftest import queue_added_columns, queue_columns
from pydantic import ValidationError

from app import models as m
from app.evals.run_settings import resolve_eval_run_settings


def S(**kw):
    kw.setdefault("name", kw.get("id", "x"))
    return kw


_K = {"columns": [{"name": "k", "type": "str", "nullable": True}]}
_KV = {"columns": [{"name": "k", "type": "str", "nullable": True},
                    {"name": "v", "type": "str", "nullable": True}]}
_QUEUE_IN = {"columns": _K["columns"] + [{"name": "score", "type": "int", "nullable": True}]}
_QUEUE_OUT = {"columns": _QUEUE_IN["columns"] + queue_added_columns()}


def _file_input(id_, tmp_path, output_schema=_K):
    return S(id=id_, type="input_data", output_schema=output_schema,
             connector={"kind": "file", "params": {"path": str(tmp_path / f"{id_}.csv")}})


def _py(id_, inputs, granularity="frame", schema=_K, **kw):
    """granularity 'row' -> python_row_function, else python_frame_function.
    `schema` is both the schema declared on every input edge and the
    output_schema — the inline transform is the identity."""
    type_ = "python_row_function" if granularity == "row" else "python_frame_function"
    return S(id=id_, type=type_, inputs=[{"id": i, "schema": schema} for i in inputs],
             function={"kind": "inline", "code": "def transform(row): return row"},
             output_schema=schema, **kw)


def _ref(path="x.csv", cols=("k",)):
    return {"path": path, "format": "csv",
            "table_schema": {"columns": [{"name": c, "type": "str", "nullable": True} for c in cols]}}


# ── is_grain_and_order_preserving (fixed by stage type) ────────────────────────────────
def test_python_frame_function_not_grain_preserving():
    assert m.parse_stage(_py("t", ["a"])).is_grain_and_order_preserving is False


def test_python_row_function_is_grain_and_order_preserving():
    assert m.parse_stage(_py("t", ["a"], granularity="row")).is_grain_and_order_preserving is True


def test_python_row_function_rejects_multiple_inputs():
    # a row function maps over one input's rows — two inputs is an enrich/expand
    with pytest.raises(ValidationError):
        m.parse_stage(S(id="t", type="python_row_function",
                                 inputs=[{"id": "a", "schema": _K}, {"id": "b", "schema": _K}],
                                 function={"kind": "inline", "code": "def transform(row): return row"},
                                 output_schema=_K))


def test_llm_is_grain_and_order_preserving():
    s = m.parse_stage(S(
        id="e", type="llm_transform",
        inputs=[{"id": "a", "schema": {"columns": [{"name": "id", "type": "str", "nullable": True}],
                                       "primary_key": ["id"]}}],
        output_schema={"columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "out", "type": "str", "nullable": True}],
                       "primary_key": ["id"]},
        llm={"prompt_template": "p"}))
    assert s.is_grain_and_order_preserving is True


def test_input_data_is_grain_and_order_preserving(tmp_path):
    assert m.parse_stage(_file_input("load", tmp_path)).is_grain_and_order_preserving is True


def test_human_review_queue_is_grain_and_order_preserving():
    # The runtime maps the queue handler per row and it emits every one of
    # them, whatever the verdict — so it is 1:1 in input order, and an eval
    # pathway through a queue stage is row-alignable.
    s = m.parse_stage(S(id="rev", type="human_review_queue",
                        inputs=[{"id": "a", "schema": _QUEUE_IN}],
                        queue=queue_columns(), output_schema=_QUEUE_OUT))
    assert s.is_grain_and_order_preserving is True


def test_publish_not_grain_and_order_preserving():
    # handle_publish runs an authored function whose output is a table of
    # artifact paths — different rows from its input, never row-alignable.
    s = m.parse_stage(S(id="pub", type="publish",
                                 inputs=[{"id": "a", "schema": _K}], publish={},
                                 function={"kind": "inline", "code": "def transform(row): return row"}))
    assert s.is_grain_and_order_preserving is False


def test_joins_and_aggregate_change_grain():
    # enrich is registered as a frame handler, so even its m:1 shape is NOT
    # grain-preserving: preservation is earned by the runtime driving the stage
    # row by row, never asserted about an operation.
    j = m.parse_stage(S(id="j", type="enrich",
                                 inputs=[{"id": "a", "schema": _K}, {"id": "b", "schema": _KV}],
                                 join={"keys": [{"left": "k", "right": "k"}], "bring": {"v": "v"}},
                                 output_schema=_K))
    x = m.parse_stage(S(id="x", type="expand",
                                 inputs=[{"id": "a", "schema": _K}, {"id": "b", "schema": _KV}],
                                 join={"keys": [{"left": "k", "right": "k"}], "bring": {"v": "v"}},
                                 output_schema=_K))
    assert x.is_grain_and_order_preserving is False
    agg_in = {"columns": [{"name": "g", "type": "str", "nullable": True}, {"name": "x", "type": "int", "nullable": True}]}
    agg = m.parse_stage(S(id="agg", type="aggregate",
                                   inputs=[{"id": "a", "schema": agg_in}],
                                   aggregate={"group_by": ["g"],
                                              "aggregations": [{"formula": "sum", "output_column": "t",
                                                                "value_column": "x"}]},
                                   output_schema={"columns": [{"name": "g", "type": "str", "nullable": True},
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
        "id": "scoring", "project": "lobbymap", "name": "n",
        "override_stage": "evidence_with_benchmarks", "target_stage": "benchmark_scoring",
        "table": _ref(cols=["evidence_id", "benchmark_id", "quote", "expected_score"]),
        "expected_outputs": [{"output_column": "score", "metric": "abs_tol", "tolerance": 1}],
    }
    base.update(over)
    return base


def test_eval_config_valid():
    c = m.EvalConfig.model_validate(_config(
        reference_overrides=[{"stage_id": "benchmark_library", "table": _ref()}],
        metrics=["mean_absolute_error"]))
    assert c.target_stage == "benchmark_scoring"
    assert c.expected_outputs[0].output_column == "score"


def test_eval_config_bad_id():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(id="Bad Id"))


def test_eval_config_override_equals_target():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(target_stage="evidence_with_benchmarks"))


def test_eval_config_nonempty_expected_outputs():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(expected_outputs=[]))


def test_eval_config_table_optional():
    # A config with no eval-dataset file is valid: expected_outputs is still required.
    cfg = m.EvalConfig.model_validate({
        "id": "e1", "project": "lobbymap", "name": "E1",
        "override_stage": "a", "target_stage": "b",
        "expected_outputs": [{"output_column": "score", "metric": "exact"}],
    })
    assert cfg.table is None


def test_eval_config_no_key_or_input_columns_fields():
    # `key` and `input_columns` are not part of the contract: the injected
    # columns are computed from override_stage's output schema, not authored.
    cfg = m.EvalConfig.model_validate({
        "id": "e1", "project": "lobbymap", "name": "E1",
        "override_stage": "a", "target_stage": "b",
        "expected_outputs": [{"output_column": "score", "metric": "exact"}],
    })
    assert not hasattr(cfg, "key")
    assert not hasattr(cfg, "input_columns")


def test_eval_config_rejects_stray_key_field():
    # extra="forbid" (app/models/schema.py _Base): a leftover `key` value
    # from an old config is a validation error, not silently-dropped data.
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate({
            "id": "e1", "project": "lobbymap", "name": "E1",
            "override_stage": "a", "target_stage": "b",
            "key": ["doc_id"],
            "expected_outputs": [{"output_column": "score", "metric": "exact"}],
        })


def test_eval_config_duplicate_reference_override():
    with pytest.raises(ValidationError):
        m.EvalConfig.model_validate(_config(
            reference_overrides=[{"stage_id": "a", "table": _ref()},
                                 {"stage_id": "a", "table": _ref()}]))


def test_eval_config_code_scorer():
    c = m.EvalConfig.model_validate(_config(code={"module": "evals.org", "function": "score"}))
    assert c.code.function == "score"


def test_expected_output_abs_tol_needs_tolerance():
    with pytest.raises(ValidationError):
        m.ExpectedOutput.model_validate({"output_column": "a", "metric": "abs_tol"})


def test_expected_output_valid_with_no_expected_field():
    c = m.ExpectedOutput.model_validate({"output_column": "a"})
    assert c.output_column == "a"
    assert not hasattr(c, "expected")


def test_expected_output_rejects_stray_expected_field():
    # extra="forbid" (app/models/schema.py _Base): a leftover `expected` value
    # from an old config is a validation error, not silently-dropped data.
    with pytest.raises(ValidationError):
        m.ExpectedOutput.model_validate({"output_column": "a", "expected": "b"})


def test_stage_output_override():
    o = m.StageOutputOverride.model_validate({"stage_id": "benchmark_library", "table": _ref()})
    assert o.stage_id == "benchmark_library"


# ── EvalRun embeds settings (no overall pass/fail) ────────────────────────────
def test_eval_run_embeds_settings():
    r = m.EvalRun.model_validate({
        "id": "run-1", "config": "scoring", "project": "lobbymap",
        "workflow_version": "abc123", "status": "scored",
        "settings": {"can_score_declaratively": True,
                     "frontier": ["benchmark_scoring"], "blocking_stages": []},
        "metrics": {"mean_absolute_error": 0.33}})
    assert r.settings.can_score_declaratively is True


def test_eval_run_has_no_passed_field():
    run = m.EvalRun.model_validate({
        "id": "r1", "config": "e1", "project": "lobbymap",
        "workflow_version": "v1", "status": "scored",
        "settings": {"can_score_declaratively": True, "frontier": ["b"], "blocking_stages": []},
        "metrics": {"match_rate": 1.0},
    })
    assert not hasattr(run, "passed")


# ── resolve_eval_run_settings on a synthetic workflow ─────────────────────────────
def _chain(tmp_path):
    """a(input) → b(row) → c(frame) → d(row)."""
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
        S(id="jn", type="expand", inputs=[{"id": "j1", "schema": _K}, {"id": "j2", "schema": _KV}],
          join={"keys": [{"left": "k", "right": "k"}], "bring": {"v": "v"}}, output_schema=_K),
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
