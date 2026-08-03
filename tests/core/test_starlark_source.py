from __future__ import annotations

import pytest
import starlark

from app.core.starlark_source import compile_starlark_module, find_bound_function


def test_a_bound_def_returns_its_name():
    module = compile_starlark_module("def transform(row):\n    return row\n", {})
    assert find_bound_function(module, ("transform",)) == "transform"


def test_an_absent_name_returns_none():
    module = compile_starlark_module("x = 1\n", {})
    assert find_bound_function(module, ("transform",)) is None


def test_a_name_bound_to_a_non_function_returns_none():
    module = compile_starlark_module("transform = 5\n", {})
    assert find_bound_function(module, ("transform",)) is None


def test_the_first_bound_name_wins_when_several_are_given():
    module = compile_starlark_module("def relabel(row):\n    return row\n", {})
    assert find_bound_function(module, ("transform", "relabel")) == "relabel"


def test_an_injected_builtin_is_callable_from_the_source():
    module = compile_starlark_module(
        "result = double(3)\n", {"double": lambda n: n * 2}
    )
    assert find_bound_function(module, ("result",)) is None  # bound, but not a function
    probe = starlark.parse("<probe>", "result")
    assert starlark.eval(module, probe, starlark.Globals.standard()) == 6


def test_a_starlark_error_that_is_not_an_unbound_variable_still_propagates():
    # Swallowing a real error here would misreport it as "not bound".
    module = compile_starlark_module("def transform(row):\n    return row\n", {})
    with pytest.raises(starlark.StarlarkError):
        find_bound_function(module, ("1bad-name",))


def test_a_builtin_must_be_injected_before_the_source_referencing_it_is_compiled():
    # Free names resolve statically at module load, so a body calling an injected
    # builtin only compiles when that builtin was passed in `builtins` up front —
    # never bound later. This is the regression guard for the injection ordering.
    source = "def transform(row):\n    return double(row['n'])\n"
    compile_starlark_module(source, {"double": lambda n: n * 2})  # does not raise
    with pytest.raises(starlark.StarlarkError):
        compile_starlark_module(source, {})
