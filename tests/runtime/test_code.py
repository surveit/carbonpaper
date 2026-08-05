"""The one module that exec's a stage's python: what it returns, and what
that code can see without importing it."""
from __future__ import annotations

import pytest

from app.models import Stage, parse_stage
from app.models.errors import StepRefused
from app.runtime.code import load_function
from app.runtime.stage_tests import run_tests_for_stage

_SCHEMA = {"columns": [{"name": "status", "type": "str", "nullable": False}]}


def test_the_named_function_is_returned():
    fn = load_function("def keep(row):\n    return True\n", "keep", "should_include")
    assert fn is not None and fn({}) is True


def test_the_default_name_is_the_fallback():
    """`function` may name a function the code does not bind; the default still wins over None."""
    fn = load_function("def should_include(row):\n    return True\n",
                       "keep", "should_include")
    assert fn is not None and fn({}) is True


def test_neither_name_bound_returns_none():
    assert load_function("x = 1\n", "keep", "should_include") is None


def test_the_code_sees_step_refused_without_importing_it():
    code = "def should_include(row):\n    raise StepRefused('cannot tell')\n"
    assert "import" not in code
    fn = load_function(code, "should_include", "should_include")
    assert fn is not None
    with pytest.raises(StepRefused):
        fn({})


# Neither caller's "not defined" error is reachable through valid code — the stage
# models reject code that binds no such name at the top level. A top-level binding
# whose VALUE is None passes that write-time check and still leaves the runtime
# with nothing to call, which is the case each message exists for.
_BINDS_NOTHING_CALLABLE = "should_include = None\n"


def _filter_stage(code: str) -> Stage:
    return parse_stage({
        "id": "keep_active", "name": "Keep active", "type": "filter_rows",
        "inputs": [{"id": "load", "schema": _SCHEMA}],
        "signature": {"form": "extends"},
        "filter": {"summary": "Keeps the rows marked active.", "code": code},
        "tests": [{"name": "keeps_an_active_row", "inputs": {"load": [{"status": "active"}]},
                   "expected": [{"status": "active"}]}],
    })


def _row_stage(code: str) -> Stage:
    return parse_stage({
        "id": "tag", "name": "Tag", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _SCHEMA}],
        "signature": {"form": "extends", "reads": [{"input": "load", "columns": _SCHEMA["columns"]}]},
        "function": {"kind": "inline", "summary": "Passes the row through.", "code": code},
        "tests": [{"name": "passes_a_row_through", "inputs": {"load": [{"status": "active"}]},
                   "expected": [{"status": "active"}]}],
    })


def test_a_filter_with_no_predicate_raises_the_filter_message():
    [result] = run_tests_for_stage(_filter_stage(_BINDS_NOTHING_CALLABLE))
    assert result.status == "error"
    assert "inline 'should_include' not defined" in (result.message or "")


def test_a_python_function_with_no_function_raises_the_python_message():
    [result] = run_tests_for_stage(_row_stage("transform = None\n"))
    assert result.status == "error"
    assert "Inline function 'transform' not defined" in (result.message or "")
