"""Tests for app/evals/compatibility.py — does an EvalConfig still fit
the stages it names, as they are right now."""
from __future__ import annotations

import pytest

from app.core import models as m
from app.evals.dataset_columns import get_injected_columns, get_output_columns_from_stage
from app.evals.compatibility import CompatibilityReport, check_eval_compatibility


def S(**kw):
    kw.setdefault("name", kw.get("id", "x"))
    return kw


def _file_input(id_, cols=("k",)):
    return m.Stage.model_validate(S(
        id=id_, type="input_data",
        connector={"kind": "file", "path": f"{id_}.csv"},
        output_schema={"columns": [{"name": c} for c in cols]}))


def _row(id_, inputs, output_schema=None, **kw):
    return m.Stage.model_validate(S(
        id=id_, type="python_row_function",
        inputs=[{"id": i} for i in inputs],
        function={"kind": "inline", "code": "def transform(row): return row"},
        output_schema=output_schema, **kw))


def _frame(id_, inputs, output_schema=None, **kw):
    return m.Stage.model_validate(S(
        id=id_, type="python_frame_function",
        inputs=[{"id": i} for i in inputs],
        function={"kind": "inline", "code": "def transform(row): return row"},
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
        "id": "scoring", "project": "lobbymap", "name": "n",
        "override_stage": "src", "target_stage": "tgt",
        "table": _ref(cols=["k", "v", "quote", "score"]),
        "expected_outputs": [{"output_column": "score", "metric": "abs_tol", "tolerance": 1}],
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
    # get_output_columns_from_stage would raise on this stage -- the
    # precondition check must catch it and report it, not let
    # check_eval_compatibility crash.
    src = _file_input("src", cols=["k", "v", "quote"])
    src = src.model_copy(update={"output_schema": None})
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    report = check_eval_compatibility(_config(), [src, tgt])
    assert isinstance(report, CompatibilityReport)
    assert report.ok is False
    assert any("declares no output schema" in p for p in report.problems)
    assert report.settings is None


def test_eval_dataset_table_missing_a_column_of_override_schema():
    # override's schema declares `extra_col`, which the eval-dataset table lacks.
    src = _file_input("src", cols=["k", "v", "quote", "extra_col"])
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    report = check_eval_compatibility(_config(), [src, tgt])
    assert report.ok is False
    assert any("extra_col" in p for p in report.problems)


def test_dataset_schema_types_shared_column_differently():
    src = m.Stage.model_validate(S(
        id="src", type="input_data",
        connector={"kind": "file", "path": "src.csv"},
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
    assert any("v" in p for p in report.problems)


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


def test_expected_output_column_not_in_target_schema():
    # The checked-column resolution inside get_injected_columns would raise
    # on this config -- the precondition check must catch it and report it,
    # not let check_eval_compatibility crash.
    config = _config(expected_outputs=[
        {"output_column": "not_emitted", "metric": "abs_tol", "tolerance": 1}])
    report = check_eval_compatibility(config, _stages())
    assert isinstance(report, CompatibilityReport)
    assert report.ok is False
    assert any("not_emitted" in p for p in report.problems)
    assert report.settings is None


def test_abs_tol_metric_on_str_typed_target_column():
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "str"}]})
    src = _file_input("src", cols=["k", "v", "quote"])
    config = _config()
    report = check_eval_compatibility(config, [src, tgt])
    assert report.ok is False
    assert any("numeric" in p for p in report.problems)


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


def test_reference_override_stage_equals_target_stage():
    # A reference override on the target stage would make resolve_eval_run_settings
    # raise; check_eval_compatibility must catch this itself and report it instead.
    config = _config(reference_overrides=[{"stage_id": "tgt", "table": _ref()}])
    report = check_eval_compatibility(config, _stages())
    assert report.ok is False
    assert any("tgt" in p for p in report.problems)
    assert report.settings is None


def test_stages_list_has_a_structural_problem():
    # A dangling input elsewhere in the stage list must not reach
    # Workflow.model_validate uncaught — it should surface as a problem string.
    src = _file_input("src", cols=["k", "v", "quote"])
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    dangling = _row("dangling", ["missing_input"])
    report = check_eval_compatibility(_config(), [src, tgt, dangling])
    assert report.ok is False
    assert any("structural problems" in p for p in report.problems)
    assert report.settings is None


def test_target_not_reachable_from_override_is_broken():
    # Two independent branches off the same input: override feeds "a",
    # target is "b", neither downstream of the other.
    src = _file_input("src", cols=["k", "v", "quote"])
    branch_a = _row("a", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    branch_b = _row("b", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    config = _config(override_stage="a", target_stage="b")
    report = check_eval_compatibility(config, [src, branch_a, branch_b])
    assert report.ok is False
    assert any("is not reachable from override" in p for p in report.problems)


def test_reachable_pathway_ok():
    report = check_eval_compatibility(_config(), _stages())
    assert report.ok is True
    assert report.problems == []


def test_table_none_does_not_crash_and_skips_file_checks():
    config = _config(table=None)
    report = check_eval_compatibility(config, _stages())
    assert report.ok is True
    assert report.settings is not None
    assert not any("eval-dataset table" in p for p in report.problems)
    assert not any("not in the eval-dataset table" in p for p in report.problems)


def test_table_none_still_catches_target_assertion_error():
    config = _config(table=None, expected_outputs=[
        {"output_column": "not_emitted", "metric": "abs_tol", "tolerance": 1}])
    report = check_eval_compatibility(config, _stages())
    assert report.ok is False
    assert any("not_emitted" in p for p in report.problems)


# ── override coverage is conflict-aware (get_injected_columns) ───────────────
def test_coverage_check_rejects_bare_name_on_a_conflicting_column():
    # override's own output includes `v`; a check also grades `v` on the
    # target -- the conflict means the eval-dataset table must carry
    # `override.v`, not a bare `v`.
    src = _file_input("src", cols=["k", "v", "quote"])
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "v", "type": "str"},
                    {"name": "score", "type": "float"}]})
    config = _config(
        expected_outputs=[{"output_column": "score", "metric": "abs_tol", "tolerance": 1},
                          {"output_column": "v", "metric": "exact"}],
        table=_ref(cols=["k", "v", "quote", "score"]))
    report = check_eval_compatibility(config, [src, tgt])
    assert report.ok is False
    assert any("override.v" in p for p in report.problems)


def test_coverage_check_accepts_conflict_aware_injected_name():
    src = _file_input("src", cols=["k", "v", "quote"])
    tgt = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "v", "type": "str"},
                    {"name": "score", "type": "float"}]})
    config = _config(
        expected_outputs=[{"output_column": "score", "metric": "abs_tol", "tolerance": 1},
                          {"output_column": "v", "metric": "exact"}],
        table=_ref(cols=["k", "override.v", "quote", "score"]))
    report = check_eval_compatibility(config, [src, tgt])
    assert report.ok is True
    assert report.problems == []


# ── get_injected_columns (the shared derivation) ──────────────────────────────
def test_get_injected_columns_no_conflict_named_after_target():
    override = _file_input("src", cols=["k", "v"])
    target = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    injected = get_injected_columns(override, target, ["score"])
    names = [c.name for c in injected]
    assert set(names) == {"k", "v"}
    assert len(names) == len(set(names))  # never a duplicate column name


def test_get_injected_columns_conflict_renames_override_side():
    override = _file_input("src", cols=["k", "score"])
    target = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    injected = get_injected_columns(override, target, ["score"])
    names = [c.name for c in injected]
    assert set(names) == {"k", "override.score"}
    assert len(names) == len(set(names))  # never a duplicate column name


def test_get_injected_columns_is_the_override_side_of_the_derivation():
    override = _file_input("src", cols=["k", "score"])
    target = _row("tgt", ["src"], output_schema={
        "columns": [{"name": "k"}, {"name": "score", "type": "float"}]})
    injected = get_injected_columns(override, target, ["score"])
    assert {c.name for c in injected} == {"k", "override.score"}


# ── fail loud: no silent degradation on a missing schema or checked column ───
def test_get_output_columns_from_stage_raises_when_stage_has_no_output_schema():
    stage = _file_input("src", cols=["k"]).model_copy(update={"output_schema": None})
    with pytest.raises(ValueError, match="declares no output schema"):
        get_output_columns_from_stage(stage)


def test_get_injected_columns_raises_for_checked_column_not_on_target():
    # An unresolvable check column is a precondition violation the caller
    # (check_eval_compatibility) must verify before calling in here -- it is
    # not silently skipped.
    override = _file_input("src", cols=["k"])
    target = _row("tgt", ["src"], output_schema={"columns": [{"name": "k"}]})
    with pytest.raises(ValueError, match="not_emitted"):
        get_injected_columns(override, target, ["not_emitted"])
