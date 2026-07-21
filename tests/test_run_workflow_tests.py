"""run_workflow_tests: aggregate a workflow's stage tests into a typed report."""
import pytest

from app.core.models import Stage
from app.runtime.stage_tests import run_workflow_tests

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": True},
]}
_DOUBLE = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"


def _row_stage(stage_id: str, tests: list[dict]) -> Stage:
    return Stage.model_validate({
        "id": stage_id, "name": stage_id, "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline", "code": _DOUBLE},
        "tests": tests,
    })


def _frame_stage(stage_id: str, tests: list[dict]) -> Stage:
    return Stage.model_validate({
        "id": stage_id, "name": stage_id, "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _IN_SCHEMA,
        "function": {"kind": "inline", "code": "def transform(df):\n    return df\n"},
        "tests": tests,
    })


def _passing_test(name: str) -> dict:
    return {"name": name, "inputs": {"load": [{"amount": 2.0}]},
            "expected": [{"amount": 2.0, "doubled": 4.0}]}


def _mismatching_test(name: str) -> dict:
    return {"name": name, "inputs": {"load": [{"amount": 2.0}]},
            "expected": [{"amount": 2.0, "doubled": 5.0}]}


def _no_tests_python_stage() -> Stage:
    return _row_stage("untested", [])


def _non_python_stage() -> Stage:
    return Stage.model_validate({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"},
    })


def _workflow() -> list[Stage]:
    return [
        _non_python_stage(),
        _row_stage("double", [_passing_test("doubles"), _mismatching_test("wrong")]),
        _frame_stage("passthrough", [{
            "name": "identity", "inputs": {"load": [{"amount": 1.0}]},
            "expected": [{"amount": 1.0}]}]),
        _no_tests_python_stage(),
    ]


def test_all_stages_run_aggregates_counts():
    report = run_workflow_tests(_workflow())
    # double (2 tests) + passthrough (1 test) run; untested has no tests, load is non-python.
    assert report.summary.stages_run == 2
    assert report.summary.tests_total == 3
    assert report.summary.passed == 2  # doubles + identity
    assert report.summary.failed == 1  # wrong
    assert {run.stage_id for run in report.stages} == {"double", "passthrough"}


def test_untested_python_stage_is_listed_not_run():
    report = run_workflow_tests(_workflow())
    assert report.untested_python_stages == ["untested"]


def test_single_stage_id_scopes_the_run():
    report = run_workflow_tests(_workflow(), stage_id="double")
    assert [run.stage_id for run in report.stages] == ["double"]
    assert report.summary.tests_total == 2
    assert report.untested_python_stages == []


def test_mismatch_surfaces_cell_diffs_in_outcome():
    report = run_workflow_tests(_workflow(), stage_id="double")
    [run] = report.stages
    failing = next(o for o in run.results if o.name == "wrong")
    assert failing.status == "mismatch"
    [diff] = failing.diffs
    assert diff.column == "doubled" and diff.expected == 5.0 and diff.actual == 4.0


def test_unknown_stage_id_raises():
    with pytest.raises(ValueError, match="no stage 'nope'"):
        run_workflow_tests(_workflow(), stage_id="nope")


def test_non_python_stage_id_raises():
    with pytest.raises(ValueError, match="does not carry runnable tests"):
        run_workflow_tests(_workflow(), stage_id="load")
