"""Tests for app/services/eval_compat.py — does an EvalConfig still fit the
stages it names, as they are right now."""
from __future__ import annotations

from app import models as m
from app.services.eval_compat import CompatibilityReport, check_eval_compatibility


def S(**kw):
    kw.setdefault("name", kw.get("id", "x"))
    return kw


def _file_input(id_, cols=("k",)):
    return m.Stage.model_validate(S(
        id=id_, type="input_data",
        connector={"kind": "file", "params": {"path": f"{id_}.csv"}},
        output_schema={"columns": [{"name": c} for c in cols]}))


def _row(id_, inputs, output_schema=None, **kw):
    return m.Stage.model_validate(S(
        id=id_, type="python_row_function",
        inputs=[{"id": i} for i in inputs],
        function={"kind": "inline", "code": "x"},
        output_schema=output_schema, **kw))


def _frame(id_, inputs, output_schema=None, **kw):
    return m.Stage.model_validate(S(
        id=id_, type="python_frame_function",
        inputs=[{"id": i} for i in inputs],
        function={"kind": "inline", "code": "x"},
        output_schema=output_schema, **kw))


def _agg(id_, inputs, output_schema=None):
    return m.Stage.model_validate(S(
        id=id_, type="aggregate", inputs=[{"id": i} for i in inputs],
        aggregate={"group_by": ["k"],
                   "aggregations": [{"formula": "sum", "output_column": "t",
                                     "value_column": "v"}]},
        output_schema=output_schema))


def _ref(path="x.csv", cols=("k",)):
    return {"path": path, "format": "csv",
            "table_schema": {"columns": [{"name": c} for c in cols]}}


def _config(**over):
    base = {
        "id": "scoring", "methodology": "lobbymap", "name": "n",
        "override_stage": "src", "target_stage": "tgt",
        "table": _ref(cols=["k", "v", "quote", "expected_score"]),
        "key": ["k"],
        "input_columns": ["quote"],
        "expected": [{"actual": "score", "expected": "expected_score",
                      "metric": "abs_tol", "tolerance": 1}],
    }
    base.update(over)
    return m.EvalConfig.model_validate(base)


# The default fixture: src(input) --v--> tgt(row), both grain-preserving, both
# schemaed with columns matching the config above.
def _stages():
    src = _file_input("src", cols=["k", "v", "quote"])
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    return [src, tgt]


def test_all_conditions_pass():
    report = check_eval_compatibility(_config(), _stages())
    assert isinstance(report, CompatibilityReport)
    assert report.ok is True
    assert report.problems == []
    assert report.settings is not None
    assert report.settings.can_score_declaratively is True


def test_unknown_override_stage():
    report = check_eval_compatibility(_config(override_stage="ghost"), _stages())
    assert report.ok is False
    assert any("ghost" in p for p in report.problems)
    assert report.settings is None


def test_unknown_target_stage():
    report = check_eval_compatibility(_config(target_stage="ghost"), _stages())
    assert report.ok is False
    assert any("ghost" in p for p in report.problems)
    assert report.settings is None


def test_unknown_reference_override_stage():
    report = check_eval_compatibility(
        _config(reference_overrides=[{"stage_id": "ghost", "table": _ref()}]),
        _stages())
    assert report.ok is False
    assert any("ghost" in p for p in report.problems)
    assert report.settings is None


def test_override_stage_has_no_output_schema():
    src = _file_input("src", cols=["k", "v", "quote"])
    src = src.model_copy(update={"output_schema": None})
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    report = check_eval_compatibility(_config(), [src, tgt])
    assert report.ok is False
    assert any("declares no output schema" in p for p in report.problems)


def test_dataset_input_columns_missing_a_column_of_override_schema():
    # override's schema declares `extra_col`, which the cases table lacks.
    src = _file_input("src", cols=["k", "v", "quote", "extra_col"])
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    report = check_eval_compatibility(_config(), [src, tgt])
    assert report.ok is False
    assert any("extra_col" in p for p in report.problems)


def test_dataset_schema_types_shared_column_differently():
    src = m.Stage.model_validate(S(
        id="src", type="input_data",
        connector={"kind": "file", "params": {"path": "src.csv"}},
        output_schema={"columns": [
            {"name": "k"}, {"name": "v", "type": "int"}, {"name": "quote"}]}))
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    config = _config(table=_ref(cols=["k", "quote", "expected_score"]))
    # override table_schema types `v` as str (default) but stage declares int.
    config = config.model_copy(update={"table": m.TableRef.model_validate({
        "path": "x.csv", "format": "csv",
        "table_schema": {"columns": [
            {"name": "k"}, {"name": "v", "type": "str"}, {"name": "quote"},
            {"name": "expected_score"}]}})})
    report = check_eval_compatibility(config, [src, tgt])
    assert report.ok is False
    assert any("v" in p and "int" in p and "str" in p for p in report.problems)


def test_reference_override_missing_a_column_of_its_stage_schema():
    src = _file_input("src", cols=["k", "v", "quote"])
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    ref_stage = _file_input("ref_stage", cols=["k", "extra"])
    config = _config(reference_overrides=[
        {"stage_id": "ref_stage", "table": _ref(cols=["k"])}])
    report = check_eval_compatibility(config, [src, tgt, ref_stage])
    assert report.ok is False
    assert any("ref_stage" in p and "extra" in p for p in report.problems)


def test_expected_actual_not_in_target_schema():
    config = _config(expected=[{"actual": "not_emitted", "expected": "expected_score",
                                "metric": "abs_tol", "tolerance": 1}])
    report = check_eval_compatibility(config, _stages())
    assert report.ok is False
    assert any("not_emitted" in p for p in report.problems)


def test_abs_tol_metric_on_str_typed_target_column():
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "str"}]})
    src = _file_input("src", cols=["k", "v", "quote"])
    config = _config()
    report = check_eval_compatibility(config, [src, tgt])
    assert report.ok is False
    assert any("numeric" in p for p in report.problems)


def test_key_column_absent_from_dataset_and_target_schema():
    src = _file_input("src", cols=["k", "v", "quote"])
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "score", "type": "float"}]})  # no `k`
    config = _config(table=_ref(cols=["v", "quote", "expected_score"]))  # no `k`
    report = check_eval_compatibility(config, [src, tgt])
    assert report.ok is False
    dataset_problems = [p for p in report.problems if "not in the cases table" in p]
    target_problems = [p for p in report.problems if "not emitted by target" in p]
    assert len(dataset_problems) == 1 and "k" in dataset_problems[0]
    assert len(target_problems) == 1 and "k" in target_problems[0]


def test_grain_blocking_stage_without_code_scorer():
    src = _file_input("src", cols=["k", "v", "quote"])
    agg = _agg("agg", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "t", "type": "int"}]})
    tgt = _row("tgt", ["agg"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    report = check_eval_compatibility(_config(), [src, agg, tgt])
    assert report.ok is False
    assert any("agg" in p for p in report.problems)


def test_grain_blocking_stage_with_code_scorer_is_not_a_problem():
    src = _file_input("src", cols=["k", "v", "quote"])
    agg = _agg("agg", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "t", "type": "int"}]})
    tgt = _row("tgt", ["agg"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    config = _config(code={"module": "evals.mod", "function": "score"})
    report = check_eval_compatibility(config, [src, agg, tgt])
    assert report.ok is True
    assert report.problems == []
