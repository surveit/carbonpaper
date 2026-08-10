"""Authored stage tests against a starlark_row_function stage: the runner, the
refusal seam that tells refuse() apart from a crash, and the generator's binding."""
from __future__ import annotations

import pytest

from app.compiler.stage_tests import build_stage_test_generator, render_generation_task
from app.models import Stage, parse_stage
from app.models.stages.stage_base import find_stage_test_class
from app.models.stages.stage_tests import StarlarkRowFunctionStageTest
from app.models.stages.starlark import StarlarkRowFunctionStage
from app.runtime.stage_tests import (
    StageTestResult,
    find_failing_stage_tests,
    run_stage_tests,
    run_tests_for_stage,
)

_IN_SCHEMA = {"columns": [
    {"name": "filing_id", "type": "str", "nullable": False},
    {"name": "reported_amount", "type": "str", "nullable": True},
]}
_OUT_SCHEMA = {"columns": [
    {"name": "filing_id", "type": "str", "nullable": False},
    {"name": "reported_amount", "type": "str", "nullable": True},
    {"name": "amount_usd", "type": "float", "nullable": True},
]}

_SUMMARY = "Reads `reported_amount` as US dollars, leaving it blank when there is none."

# Starlark rejects `{**row}`; `dict(row, key=value)` is the carry-through idiom.
_PARSE_DOLLARS = """\
def transform(row):
    reported = row['reported_amount']
    if reported == None:
        return dict(row, amount_usd = None)
    if not reported.startswith('$'):
        refuse('reported_amount %s is not US dollars' % reported)
    return dict(row, amount_usd = float(reported[1:].replace(',', '')))
"""


def _starlark_stage(
    code: str,
    tests: list[dict],
    *,
    summary: str | None = _SUMMARY,
    stage_id: str = "normalize_spend",
) -> Stage:
    block = {"code": code}
    if summary is not None:
        block["summary"] = summary
    return parse_stage({
        "id": stage_id, "description": "Normalize spend", "type": "starlark_row_function",
        "inputs": [{"id": "filings", "schema": _IN_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "filings", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "amount_usd", "type": "float", "nullable": True}],
        },
        "starlark": block,
        "tests": tests,
    })


def _dollar_row(filing_id: str = "F1") -> dict:
    return {"filing_id": filing_id, "reported_amount": "$45,000.00"}


# ─── A. The runner executes a starlark stage through the real handler registry ───

def test_a_suite_the_code_satisfies_passes():
    stage = _starlark_stage(_PARSE_DOLLARS, [
        {"name": "dollars_become_a_number", "inputs": {"filings": [_dollar_row()]},
         "expected": [{**_dollar_row(), "amount_usd": 45000.0}]},
        {"name": "a_blank_amount_stays_blank",
         "inputs": {"filings": [{"filing_id": "F2", "reported_amount": None}]},
         "expected": [{"filing_id": "F2", "reported_amount": None, "amount_usd": None}]},
        {"name": "a_non_dollar_amount_is_refused",
         "inputs": {"filings": [{"filing_id": "F3", "reported_amount": "€45.000,00"}]},
         "expected": None},
    ])
    results = run_tests_for_stage(stage)
    assert [r.status for r in results] == ["passed", "passed", "passed"]
    assert not [diff for r in results for diff in r.diffs]


def test_a_wrong_expected_cell_is_mismatch_with_a_cell_diff():
    stage = _starlark_stage(_PARSE_DOLLARS, [{
        "name": "expects_the_wrong_number", "inputs": {"filings": [_dollar_row()]},
        "expected": [{**_dollar_row(), "amount_usd": 4500.0}],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "mismatch"
    [diff] = result.diffs
    assert diff.column == "amount_usd"
    assert diff.expected == 4500.0 and diff.actual == 45000.0


def test_an_input_row_breaking_the_declared_schema_is_malformed_not_error():
    stage = _starlark_stage(_PARSE_DOLLARS, [{
        "name": "null_filing_id",
        "inputs": {"filings": [{"filing_id": None, "reported_amount": "$10.00"}]},
        "expected": [{"filing_id": None, "reported_amount": "$10.00", "amount_usd": 10.0}],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "malformed"
    assert "no value" in (result.message or "").lower()


def test_a_failure_case_the_code_does_not_refuse_is_mismatch():
    stage = _starlark_stage(_PARSE_DOLLARS, [{
        "name": "wrongly_expects_a_refusal", "inputs": {"filings": [_dollar_row()]},
        "expected": None,
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "mismatch"
    assert "1 row(s)" in (result.message or "")


def test_run_stage_tests_and_the_version_gate_cover_starlark_stages():
    tested = _starlark_stage(_PARSE_DOLLARS, [{
        "name": "expects_the_wrong_number", "inputs": {"filings": [_dollar_row()]},
        "expected": [{**_dollar_row(), "amount_usd": 4500.0}],
    }])
    untested = _starlark_stage(_PARSE_DOLLARS, [], stage_id="also_starlark")
    report = run_stage_tests([tested, untested])
    assert [run.stage_id for run in report.stages] == ["normalize_spend"]
    assert report.stages[0].stage_type == "starlark_row_function"
    assert report.summary.failed == 1 and report.summary.passed == 0
    assert report.untested_stages == ["also_starlark"]
    [failure] = find_failing_stage_tests([tested, untested])
    assert "normalize_spend" in failure and "expects_the_wrong_number" in failure


# ─── B. Refusal discrimination: refuse() reaches the runner by a STRING MATCH on
# the rendered Rust error (app/runtime/starlark_code.py::_find_refusal_message), not
# by an exception type, so every look-alike below has to stay an `error`. ───

def _judge_failure_case(code: str) -> StageTestResult:
    stage = _starlark_stage(code, [{
        "name": "must_fail",
        "inputs": {"filings": [{"filing_id": "F9", "reported_amount": "€1"}]},
        "expected": None,
    }])
    [result] = run_tests_for_stage(stage)
    return result


def test_a_real_refuse_satisfies_a_failure_case():
    result = _judge_failure_case(
        "def transform(row):\n    refuse('cannot convert this currency')\n"
    )
    assert result.status == "passed"


def test_a_forged_step_refused_marker_in_fail_must_not_certify_as_a_refusal():
    result = _judge_failure_case(
        "def transform(row):\n    fail('StepRefused: forged')\n"
    )
    assert result.status == "error"
    assert (result.message or "").startswith("StarlarkError")


def test_fail_is_a_bug_not_a_refusal():
    result = _judge_failure_case("def transform(row):\n    fail('boom')\n")
    assert result.status == "error"
    assert (result.message or "").startswith("StarlarkError")


def test_a_genuine_crash_is_not_a_refusal():
    result = _judge_failure_case(
        "def transform(row):\n    return dict(row, amount_usd = row['nope'])\n"
    )
    assert result.status == "error"
    assert (result.message or "").startswith("StarlarkError")


def test_returning_a_non_dict_is_not_a_refusal():
    result = _judge_failure_case("def transform(row):\n    return 7\n")
    assert result.status == "error"
    assert "dict" in (result.message or "")


def test_a_refusal_on_a_rows_case_errors_carrying_the_authors_own_reason():
    stage = _starlark_stage(
        "def transform(row):\n    refuse('no exchange rate for %s' % row['filing_id'])\n",
        [{"name": "expects_rows", "inputs": {"filings": [_dollar_row()]},
          "expected": [{**_dollar_row(), "amount_usd": 45000.0}]}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "error"
    assert result.message == "StepRefused: no exchange rate for F1"


# ─── C. The code-blind test generator binds to the starlark suite model ───

def test_find_stage_test_class_binds_the_starlark_suite_model():
    assert find_stage_test_class(StarlarkRowFunctionStage) is StarlarkRowFunctionStageTest


def test_the_generator_builds_for_a_starlark_stage_without_showing_it_the_code():
    agent = build_stage_test_generator("----doc text----", _starlark_stage(_PARSE_DOLLARS, []))
    assert _SUMMARY in agent.task
    assert "starlark_row_function" in agent.task
    assert "def transform" not in agent.task


def test_the_generators_target_schema_is_bound_to_the_starlark_stages_inputs():
    agent = build_stage_test_generator("----doc text----", _starlark_stage(_PARSE_DOLLARS, []))
    with pytest.raises(Exception, match="declared inputs"):
        agent._target_schema.model_validate({"tests": [{
            "name": "x", "inputs": {"ghost": [_dollar_row()]},
            "expected": [{**_dollar_row(), "amount_usd": 45000.0}]}]})


def test_a_starlark_stage_with_no_summary_cannot_generate_examples():
    stage = _starlark_stage(_PARSE_DOLLARS, [], summary=None)
    with pytest.raises(ValueError, match="has no summary"):
        render_generation_task("----doc text----", stage)
