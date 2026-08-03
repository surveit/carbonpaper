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
    # `type = 5` shadows the probe's own builtin, so `type(transform)` raises
    # "Operation `call()` not supported on type `int`" — a real StarlarkError,
    # not an unbound-variable one. Swallowing it here would misreport it as
    # "not bound" instead of surfacing the actual error.
    module = compile_starlark_module("type = 5\ndef transform(row):\n    return row\n", {})
    with pytest.raises(starlark.StarlarkError):
        find_bound_function(module, ("transform",))


def test_a_non_identifier_name_is_rejected_rather_than_read_as_unbound():
    # function_name is stage config an author writes, not a trusted literal. A
    # crafted non-identifier string can otherwise make the probe expression
    # evaluate to something other than a plain name lookup.
    module = compile_starlark_module("def transform(row):\n    return row\n", {})
    with pytest.raises(ValueError):
        find_bound_function(module, ('transform) if False else ("function"',))


def test_a_non_identifier_name_cannot_execute_a_builtin_during_the_probe():
    calls = []
    module = compile_starlark_module(
        "def transform(row):\n    return row\n", {"refuse": lambda reason: calls.append(reason)}
    )
    with pytest.raises(ValueError):
        find_bound_function(module, ('refuse("side effect")) or (1',))
    assert calls == []


def test_a_builtin_must_be_injected_before_the_source_referencing_it_is_compiled():
    # Free names resolve statically at module load, so a body calling an injected
    # builtin only compiles when that builtin was passed in `builtins` up front —
    # never bound later. This is the regression guard for the injection ordering.
    source = "def transform(row):\n    return double(row['n'])\n"
    compile_starlark_module(source, {"double": lambda n: n * 2})  # does not raise
    with pytest.raises(starlark.StarlarkError):
        compile_starlark_module(source, {})
