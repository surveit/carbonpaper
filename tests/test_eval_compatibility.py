"""Tests for app/evals/compatibility.py — does an EvalConfig still fit
the stages it names, as they are right now."""
from __future__ import annotations

import pytest

from app import models as m
from app.models.records.eval_config import EvalConfig
from app.evals.dataset_columns import get_injected_columns, get_output_columns_from_stage
from app.evals import compatibility
from app.evals.compatibility import CompatibilityReport
from app.models.workflow_stage import WorkflowStage


def validate_eval_compatibility(config, stages) -> CompatibilityReport:
    """Test shim: the app builds the workflow at its caller; these cases author stage lists."""
    return compatibility.validate_eval_compatibility(config, m.Workflow(stages=list(stages)))


def S(**kw):
    kw.setdefault("description", kw.get("id", "x"))
    return kw


def _file_input(id_, tmp_path, cols=("k",)):
    return m.parse_stage(S(
        id=id_, type="input_data",
        connector={"kind": "file", "params": {"path": str(tmp_path / f"{id_}.csv")}},
        signature={"form": "replaces",
                   "produces": [{"name": c, "type": "str", "nullable": True} for c in cols]}))


def _input_refs(inputs):
    return [
        {"id": upstream.id if isinstance(upstream, m.AbstractStage) else upstream[0]}
        for upstream in inputs
    ]


def _anchor_columns(inputs):
    """Every upstream these tests build is replaces-form, so its output IS `produces`."""
    if not inputs:
        return []
    first = inputs[0]
    if isinstance(first, m.AbstractStage):
        return [c.model_dump() for c in first.signature.produces]
    return [{"name": c, "type": "str", "nullable": True} for c in first[1]]


def _extends(inputs, output_schema):
    """A row function only adds, so the adds are what `output_schema` names beyond the anchor."""
    flowing = {c["name"] for c in _anchor_columns(inputs)}
    added = [c for c in (output_schema or {}).get("columns", []) if c["name"] not in flowing]
    return {"form": "extends", "adds": added}


def _row(id_, inputs, output_schema=None, **kw):
    return m.parse_stage(S(
        id=id_, type="python_row_function", inputs=_input_refs(inputs),
        function={"kind": "inline", "code": "def transform(row): return row"},
        signature=_extends(inputs, output_schema), **kw))


def _frame(id_, inputs, output_schema=None, **kw):
    return m.parse_stage(S(
        id=id_, type="python_frame_function",
        inputs=_input_refs(inputs),
        function={"kind": "inline", "code": "def transform(row): return row"},
        signature={"form": "replaces",
                   "produces": (output_schema or {}).get("columns", [])}, **kw))


def _agg(id_, inputs, output_schema=None):
    return m.parse_stage(S(
        id=id_, type="aggregate", inputs=_input_refs(inputs),
        aggregate={"group_by": ["k"],
                   "aggregations": [{"formula": "sum", "output_column": "t",
                                     "value_column": "v"}]},
        signature={"form": "replaces",
                   "reads": [{"input": _input_refs(inputs)[0]["id"], "columns": [
                       {"name": "k", "type": "str", "nullable": True},
                       {"name": "v", "type": "str", "nullable": True}]}],
                   "produces": (output_schema or {}).get("columns", [])}))


def _ref(path="x.csv", cols=("k",)):
    return {"path": path, "format": "csv",
            "table_schema": {"columns": [{"name": c, "type": "str", "nullable": True} for c in cols]}}


def _config(**over):
    base = {
        "eval_id": "scoring", "project": "lobbymap", "name": "n",
        "override_stage": "src", "target_stage": "tgt",
        "table": _ref(cols=["k", "v", "quote", "score"]),
        "expected_outputs": [{"output_column": "score", "metric": "abs_tol", "tolerance": 1}],
    }
    base.update(over)
    return EvalConfig.model_validate(base)


# The default fixture: src(input) --v--> tgt(row), both grain-preserving, both
# schemaed with columns matching the config above.
def _stages(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    return [src, tgt]


def test_all_conditions_pass(tmp_path):
    report = validate_eval_compatibility(_config(), _stages(tmp_path))
    assert isinstance(report, CompatibilityReport)
    assert report.ok is True
    assert report.problems == []
    assert report.settings is not None
    assert report.settings.can_score_declaratively is True


def test_unknown_override_stage(tmp_path):
    report = validate_eval_compatibility(_config(override_stage="ghost"), _stages(tmp_path))
    assert report.ok is False
    assert any("ghost" in p for p in report.problems)
    assert report.settings is None


def test_unknown_target_stage(tmp_path):
    report = validate_eval_compatibility(_config(target_stage="ghost"), _stages(tmp_path))
    assert report.ok is False
    assert any("ghost" in p for p in report.problems)
    assert report.settings is None


def test_unknown_reference_override_stage(tmp_path):
    report = validate_eval_compatibility(
        _config(reference_overrides=[{"stage_id": "ghost", "table": _ref()}]),
        _stages(tmp_path))
    assert report.ok is False
    assert any("ghost" in p for p in report.problems)
    assert report.settings is None


def test_an_override_stage_with_no_output_schema_is_reported_not_crashed_on(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    pub = m.parse_stage(S(
        id="pub", type="report", inputs=_input_refs([src]),
        report={"format": "json"}, signature={"form": "replaces"},
        function={"kind": "inline", "code": "def transform(df, output_dir): return df"}))
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    report = validate_eval_compatibility(_config(override_stage="pub"), [src, pub, tgt])
    assert isinstance(report, CompatibilityReport)
    assert report.ok is False
    assert any("declares no output schema" in p for p in report.problems)
    assert report.settings is None


def test_eval_dataset_table_missing_a_column_of_override_schema(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote", "extra_col"])
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    report = validate_eval_compatibility(_config(), [src, tgt])
    assert report.ok is False
    assert any("extra_col" in p for p in report.problems)


def test_dataset_schema_types_shared_column_differently(tmp_path):
    src = m.parse_stage(S(
        id="src", type="input_data",
        connector={"kind": "file", "params": {"path": str(tmp_path / "src.csv")}},
        signature={
            "form": "replaces",
            "produces": [
                {"name": "k", "type": "str", "nullable": True},
                {"name": "v", "type": "int", "nullable": True},
                {"name": "quote", "type": "str", "nullable": True},
            ],
        }))
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    config = _config(table=_ref(cols=["k", "quote", "expected_score"]))
    # override table_schema types `v` as str (default) but stage declares int.
    config = config.model_copy(update={"table": m.TableRef.model_validate({
        "path": "x.csv", "format": "csv",
        "table_schema": {"columns": [
            {"name": "k", "type": "str", "nullable": True}, {"name": "v", "type": "str", "nullable": True}, {"name": "quote", "type": "str", "nullable": True},
            {"name": "expected_score", "type": "str", "nullable": True}]}})})
    report = validate_eval_compatibility(config, [src, tgt])
    assert report.ok is False
    assert any("v" in p for p in report.problems)


def test_reference_override_missing_a_column_of_its_stage_schema(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    ref_stage = _file_input("ref_stage", tmp_path, cols=["k", "extra"])
    config = _config(reference_overrides=[
        {"stage_id": "ref_stage", "table": _ref(cols=["k"])}])
    report = validate_eval_compatibility(config, [src, tgt, ref_stage])
    assert report.ok is False
    assert any("ref_stage" in p and "extra" in p for p in report.problems)


def test_an_expected_output_column_not_on_the_target_is_reported_not_crashed_on(tmp_path):
    config = _config(expected_outputs=[
        {"output_column": "not_emitted", "metric": "abs_tol", "tolerance": 1}])
    report = validate_eval_compatibility(config, _stages(tmp_path))
    assert isinstance(report, CompatibilityReport)
    assert report.ok is False
    assert any("not_emitted" in p for p in report.problems)
    assert report.settings is None


def test_abs_tol_metric_on_str_typed_target_column(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "str", "nullable": True}]})
    config = _config()
    report = validate_eval_compatibility(config, [src, tgt])
    assert report.ok is False
    assert any("numeric" in p for p in report.problems)


def test_grain_blocking_stage_without_code_scorer(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    agg = _agg("agg", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "t", "type": "str", "nullable": True}]})
    tgt = _row("tgt", [agg], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    report = validate_eval_compatibility(_config(), [src, agg, tgt])
    assert report.ok is False
    assert any("agg" in p for p in report.problems)


def test_grain_blocking_stage_with_code_scorer_is_not_a_problem(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    agg = _agg("agg", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "t", "type": "str", "nullable": True}]})
    tgt = _row("tgt", [agg], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    config = _config(code={"module": "evals.mod", "function": "score"})
    report = validate_eval_compatibility(config, [src, agg, tgt])
    assert report.ok is True
    assert report.problems == []


def test_a_reference_override_on_the_target_stage_is_reported_not_crashed_on(tmp_path):
    config = _config(reference_overrides=[{"stage_id": "tgt", "table": _ref()}])
    report = validate_eval_compatibility(config, _stages(tmp_path))
    assert report.ok is False
    assert any("tgt" in p for p in report.problems)
    assert report.settings is None


def test_a_dangling_input_elsewhere_means_no_workflow_reaches_this_check(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    dangling = _row("dangling", [("missing_input", ["k"])],
                    output_schema={"columns": [{"name": "k", "type": "str", "nullable": True}]})
    not_formed = m.build_workflow([src, tgt, dangling])
    assert isinstance(not_formed, m.WorkflowNotFormed)
    assert any("input `missing_input` references no stage" in issue
               for issue in not_formed.issues)


def test_target_not_reachable_from_override_is_broken(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    branch_a = _row("a", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    branch_b = _row("b", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    config = _config(override_stage="a", target_stage="b")
    report = validate_eval_compatibility(config, [src, branch_a, branch_b])
    assert report.ok is False
    assert any("is not reachable from override" in p for p in report.problems)


def test_reachable_pathway_ok(tmp_path):
    report = validate_eval_compatibility(_config(), _stages(tmp_path))
    assert report.ok is True
    assert report.problems == []


def test_table_none_does_not_crash_and_skips_file_checks(tmp_path):
    config = _config(table=None)
    report = validate_eval_compatibility(config, _stages(tmp_path))
    assert report.ok is True
    assert report.settings is not None
    assert not any("eval-dataset table" in p for p in report.problems)
    assert not any("not in the eval-dataset table" in p for p in report.problems)


def test_table_none_still_catches_target_assertion_error(tmp_path):
    config = _config(table=None, expected_outputs=[
        {"output_column": "not_emitted", "metric": "abs_tol", "tolerance": 1}])
    report = validate_eval_compatibility(config, _stages(tmp_path))
    assert report.ok is False
    assert any("not_emitted" in p for p in report.problems)


# ── override coverage is conflict-aware (get_injected_columns) ───────────────
def test_coverage_check_rejects_bare_name_on_a_conflicting_column(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "v", "type": "str", "nullable": True},
                    {"name": "score", "type": "float", "nullable": True}]})
    config = _config(
        expected_outputs=[{"output_column": "score", "metric": "abs_tol", "tolerance": 1},
                          {"output_column": "v", "metric": "exact"}],
        table=_ref(cols=["k", "v", "quote", "score"]))
    report = validate_eval_compatibility(config, [src, tgt])
    assert report.ok is False
    assert any("override.v" in p for p in report.problems)


def test_coverage_check_accepts_conflict_aware_injected_name(tmp_path):
    src = _file_input("src", tmp_path, cols=["k", "v", "quote"])
    tgt = _row("tgt", [src], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "v", "type": "str", "nullable": True},
                    {"name": "score", "type": "float", "nullable": True}]})
    config = _config(
        expected_outputs=[{"output_column": "score", "metric": "abs_tol", "tolerance": 1},
                          {"output_column": "v", "metric": "exact"}],
        table=_ref(cols=["k", "override.v", "quote", "score"]))
    report = validate_eval_compatibility(config, [src, tgt])
    assert report.ok is True
    assert report.problems == []


# ── get_injected_columns (the shared column rule) ──────────────────────────────
def test_get_injected_columns_no_conflict_named_after_target(tmp_path):
    override = _file_input("src", tmp_path, cols=["k", "v"])
    target = _row("tgt", [override], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    placed = m.Workflow(stages=[override, target]).index_workflow_stages_by_id()
    injected = get_injected_columns(placed["src"], placed["tgt"], ["score"])
    names = [c.name for c in injected]
    assert set(names) == {"k", "v"}
    assert len(names) == len(set(names))  # never a duplicate column name


def test_get_injected_columns_conflict_renames_override_side(tmp_path):
    override = _file_input("src", tmp_path, cols=["k", "score"])
    target = _row("tgt", [override], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    placed = m.Workflow(stages=[override, target]).index_workflow_stages_by_id()
    injected = get_injected_columns(placed["src"], placed["tgt"], ["score"])
    names = [c.name for c in injected]
    assert set(names) == {"k", "override.score"}
    assert len(names) == len(set(names))  # never a duplicate column name


def test_get_injected_columns_is_the_override_side_of_the_column_rule(tmp_path):
    override = _file_input("src", tmp_path, cols=["k", "score"])
    target = _row("tgt", [override], output_schema={
        "columns": [{"name": "k", "type": "str", "nullable": True}, {"name": "score", "type": "float", "nullable": True}]})
    placed = m.Workflow(stages=[override, target]).index_workflow_stages_by_id()
    injected = get_injected_columns(placed["src"], placed["tgt"], ["score"])
    assert {c.name for c in injected} == {"k", "override.score"}


# ── fail loud: no silent degradation on a missing schema or checked column ───
def test_get_output_columns_from_stage_raises_when_stage_has_no_output_schema(tmp_path):
    stage = _file_input("src", tmp_path, cols=["k"])
    emits_nothing = WorkflowStage(stage=stage, inputs=[], output_schema=None)
    with pytest.raises(ValueError, match="declares no output schema"):
        get_output_columns_from_stage(emits_nothing)


def test_get_injected_columns_raises_for_checked_column_not_on_target(tmp_path):
    override = _file_input("src", tmp_path, cols=["k"])
    target = _row("tgt", [override], output_schema={"columns": [{"name": "k", "type": "str", "nullable": True}]})
    placed = m.Workflow(stages=[override, target]).index_workflow_stages_by_id()
    with pytest.raises(ValueError, match="not_emitted"):
        get_injected_columns(placed["src"], placed["tgt"], ["not_emitted"])
