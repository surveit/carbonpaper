"""filter_rows carries runnable stage tests: the drop case, the refusal case, and
the one-row-in arity a filter is held to."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage, parse_stage
from app.runtime.stage_tests import (
    find_failing_stage_tests,
    run_stage_tests,
    run_tests_for_stage,
)
from conftest import reads_of

# A filter passes rows through unchanged, so its output schema equals its input's.
_SCHEMA = {"columns": [{"name": "status", "type": "str", "nullable": False}]}

_KEEP_ACTIVE = "def should_include(row):\n    return row['status'] == 'active'\n"



def _filter_stage(code: str, tests: list[dict], stage_id: str = "keep_active") -> Stage:
    return parse_stage({
        "id": stage_id, "description": "Keep active", "type": "filter_rows",
        "inputs": [{"id": "load", "schema": _SCHEMA}],
        "signature": {"form": "extends", "reads": reads_of("load", _SCHEMA["columns"])},
        "filter": {"summary": "Keeps the rows marked active.", "code": code},
        "tests": tests,
    })


def test_kept_row_passes():
    stage = _filter_stage(_KEEP_ACTIVE, [{
        "name": "keeps_an_active_row", "inputs": {"load": [{"status": "active"}]},
        "expected": [{"status": "active"}],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed" and not result.diffs


def test_dropped_row_passes_with_zero_expected_rows():
    """One row in, NO row out — the case a python_row_function test rejects as fan-in."""
    stage = _filter_stage(_KEEP_ACTIVE, [{
        "name": "drops_a_closed_row", "inputs": {"load": [{"status": "closed"}]},
        "expected": [],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed" and not result.diffs


def test_a_drop_is_not_read_as_a_refusal():
    """Kept nothing (`[]`) must not satisfy a test claiming the step fails (`null`)."""
    stage = _filter_stage(_KEEP_ACTIVE, [{
        "name": "expects_a_refusal", "inputs": {"load": [{"status": "closed"}]},
        "expected": None,
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "mismatch"


def test_two_input_rows_are_rejected():
    with pytest.raises(ValidationError, match="at most 1 item"):
        _filter_stage(_KEEP_ACTIVE, [{
            "name": "two_rows_in",
            "inputs": {"load": [{"status": "active"}, {"status": "closed"}]},
            "expected": [{"status": "active"}],
        }])


def test_two_expected_rows_out_of_one_are_rejected():
    """A filter cannot fan out — it keeps the row it was given or drops it."""
    with pytest.raises(ValidationError, match="at most 1 item"):
        _filter_stage(_KEEP_ACTIVE, [{
            "name": "fans_out", "inputs": {"load": [{"status": "active"}]},
            "expected": [{"status": "active"}, {"status": "active"}],
        }])


# No import line: the runtime seeds StepRefused into the predicate's namespace, and
# this is the only place that is exercised through the real filter handler.
_REFUSES = (
    "def should_include(row):\n"
    "    raise StepRefused('status is blank: cannot tell active from closed')\n"
)


def test_predicate_refuses_without_importing_step_refused():
    """Pins the namespace seeding: unseeded, this same code dies with NameError."""
    assert "import" not in _REFUSES
    stage = _filter_stage(_REFUSES, [{
        "name": "refuses_a_blank_status", "inputs": {"load": [{"status": ""}]},
        "expected": None,
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"
    assert "NameError" not in (result.message or "")


def test_a_predicate_raising_something_else_is_an_error_not_a_refusal():
    """A KeyError is the predicate falling over, not refusing — it must not certify."""
    stage = _filter_stage(
        "def should_include(row):\n    raise KeyError('state')\n",
        [{"name": "refuses_a_blank_status", "inputs": {"load": [{"status": ""}]},
          "expected": None}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "error"
    assert "KeyError" in (result.message or "") and "state" in (result.message or "")


def test_run_stage_tests_covers_filter_stages():
    tested = _filter_stage(_KEEP_ACTIVE, [{
        "name": "keeps_an_active_row", "inputs": {"load": [{"status": "active"}]},
        "expected": [{"status": "active"}],
    }])
    untested = _filter_stage(_KEEP_ACTIVE, [], stage_id="also_a_filter")
    report = run_stage_tests([tested, untested])
    assert [run.stage_id for run in report.stages] == ["keep_active"]
    assert report.stages[0].stage_type == "filter_rows"
    assert report.summary.passed == 1 and report.summary.failed == 0
    assert report.untested_stages == ["also_a_filter"]


def test_find_failing_stage_tests_reports_a_failing_filter():
    stage = _filter_stage(_KEEP_ACTIVE, [{
        "name": "wrongly_expects_the_row_kept",
        "inputs": {"load": [{"status": "closed"}]},
        "expected": [{"status": "closed"}],
    }])
    [failure] = find_failing_stage_tests([stage])
    assert "keep_active" in failure and "wrongly_expects_the_row_kept" in failure
