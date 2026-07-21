"""measure_stage_test_coverage: branch coverage over a stage's transform,
scoped to just that function, computed from the SAME test execution path
run_stage_tests uses (see app/runtime/stage_coverage.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.core.models import Stage
from app.runtime.stage_coverage import measure_stage_test_coverage

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "big", "type": "bool", "nullable": False},
]}

# `if/else` on one line each: line 2 is the branch point (2 exits — into the
# `if` body, into the `else` body).
_BRANCHING_CODE = (
    "def transform(row):\n"
    "    if row['amount'] > 10:\n"
    "        return {**row, 'big': True}\n"
    "    else:\n"
    "        return {**row, 'big': False}\n"
)

_STRAIGHT_LINE_CODE = "def transform(row):\n    return {**row, 'big': False}\n"


def _row_stage(code: str, tests: list[dict], *, module: str | None = None) -> Stage:
    function = (
        {"kind": "module", "module": module}
        if module is not None
        else {"kind": "inline", "code": code}
    )
    return Stage.model_validate({
        "id": "classify", "name": "Classify", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": function,
        "tests": tests,
    })


def _test_case(name: str, amount: float, big: bool) -> dict:
    return {
        "name": name, "inputs": {"load": [{"amount": amount}]},
        "expected": [{"amount": amount, "big": big}],
    }


def test_both_branches_tested_reports_100_percent():
    stage = _row_stage(_BRANCHING_CODE, [
        _test_case("above_threshold", 20.0, True),
        _test_case("at_or_below_threshold", 5.0, False),
    ])
    report = measure_stage_test_coverage(stage)
    assert report.test_count == 2
    assert report.branch_percent == 100.0
    assert report.covered_branches == report.total_branches
    assert report.uncovered_branches == []
    assert report.uncovered_lines == []


def test_untested_branch_reports_partial_coverage_and_identifies_it():
    # Only the True arm is exercised — the `else` is never reached.
    stage = _row_stage(_BRANCHING_CODE, [_test_case("above_threshold", 20.0, True)])
    report = measure_stage_test_coverage(stage)
    assert report.test_count == 1
    assert report.branch_percent < 100.0
    assert report.total_branches > 0
    assert report.covered_branches < report.total_branches
    [branch] = report.uncovered_branches
    # line 2 is `if row['amount'] > 10:` in _BRANCHING_CODE — the decision point
    # with the untaken exit.
    assert branch.line == 2
    assert branch.branches_taken < branch.branches_total
    # The `else` body (line 5) never ran at all.
    assert 5 in report.uncovered_lines


def test_no_tests_reports_zero_coverage_over_existing_branches():
    stage = _row_stage(_BRANCHING_CODE, [])
    report = measure_stage_test_coverage(stage)
    assert report.test_count == 0
    assert report.branch_percent == 0.0
    assert report.covered_branches == 0
    assert report.total_branches > 0


def test_branchless_function_is_vacuously_100_percent():
    stage = _row_stage(_STRAIGHT_LINE_CODE, [_test_case("only_case", 1.0, False)])
    report = measure_stage_test_coverage(stage)
    assert report.total_branches == 0
    assert report.branch_percent == 100.0
    assert report.uncovered_branches == []


def test_module_kind_stage_is_measured_from_its_real_source_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module_name = "stage_coverage_fixture_module"
    (tmp_path / f"{module_name}.py").write_text(_BRANCHING_CODE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module_name, None)
    try:
        stage = _row_stage(_BRANCHING_CODE, [
            _test_case("above_threshold", 20.0, True),
            _test_case("at_or_below_threshold", 5.0, False),
        ], module=module_name)
        report = measure_stage_test_coverage(stage)
        assert report.branch_percent == 100.0
        assert report.uncovered_branches == []
    finally:
        sys.modules.pop(module_name, None)


def test_coverage_is_scoped_to_the_transform_function_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A module-kind stage sharing a file with an unrelated helper: the helper's
    # own untaken branch must not count against (or for) THIS stage's claim.
    module_name = "stage_coverage_scoping_fixture"
    source = (
        "def unrelated_helper(x):\n"
        "    if x:\n"
        "        return 1\n"
        "    else:\n"
        "        return 2\n"
        "\n"
        "def transform(row):\n"
        "    return {**row, 'big': False}\n"
    )
    (tmp_path / f"{module_name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module_name, None)
    try:
        stage = _row_stage(source, [_test_case("only_case", 1.0, False)], module=module_name)
        report = measure_stage_test_coverage(stage)
        assert report.total_branches == 0
        assert report.branch_percent == 100.0
        assert report.uncovered_lines == []
    finally:
        sys.modules.pop(module_name, None)


def test_stage_type_without_tests_support_raises():
    stage = Stage.model_validate({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"},
    })
    with pytest.raises(ValueError):
        measure_stage_test_coverage(stage)
