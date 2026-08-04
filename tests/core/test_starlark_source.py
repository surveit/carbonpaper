from __future__ import annotations

import pytest
import starlark

from app.core.starlark_source import (
    DEFAULT_FUNCTION_NAME,
    REFUSE_BUILTIN,
    compile_starlark_module,
    find_bound_function,
)


def test_the_module_docstring_says_loading_source_executes_it():
    # Regression: `compile_starlark_module` (via `starlark.eval`) runs the
    # source's top-level statements at "compile" time — a module docstring
    # describing this as mere compilation would be false.
    import app.core.starlark_source as module

    assert "execute" in (module.__doc__ or "").lower()


def test_the_model_and_runtime_layers_import_the_same_default_function_name():
    # Regression: the model layer (write-time validation) and the runtime layer
    # (execution) each once declared "transform" as their own private constant,
    # linked only by a comment. Both must import this one definition so they
    # cannot drift apart.
    from app.models.stages.starlark import DEFAULT_FUNCTION_NAME as model_name
    from app.runtime.stages.starlark_functions import DEFAULT_FUNCTION_NAME as runtime_name

    assert model_name is DEFAULT_FUNCTION_NAME
    assert runtime_name is DEFAULT_FUNCTION_NAME


def test_the_model_and_runtime_layers_import_the_same_refuse_builtin_name():
    # Regression: "refuse" was likewise declared twice — once in the model
    # layer's write-time validation, once in the runtime's execution path.
    from app.models.stages.starlark import REFUSE_BUILTIN as model_refuse
    from app.runtime.starlark_code import REFUSE_BUILTIN as runtime_refuse

    assert model_refuse is REFUSE_BUILTIN
    assert runtime_refuse is REFUSE_BUILTIN


def test_a_bound_def_returns_its_name():
    module = compile_starlark_module("def transform(row):\n    return row\n", {})
    assert find_bound_function(module, ("transform",)) == "transform"


def test_an_absent_name_returns_none():
    module = compile_starlark_module("x = 1\n", {})
    assert find_bound_function(module, ("transform",)) is None


@pytest.mark.parametrize("name", ["len", "dict", "fail", "str", "type", "sorted", "range"])
def test_a_standard_global_the_module_never_bound_returns_none(name):
    # Regression: the probe once evaluated `type(name)` against the standard
    # globals, so a name the STANDARD LIBRARY provides (never bound by `code`
    # itself) read as bound — a stage saved even though it defines nothing.
    module = compile_starlark_module("x = 1\n", {})
    assert find_bound_function(module, (name,)) is None


def test_a_module_level_def_shadowing_a_standard_global_is_still_found():
    # The fix for the above must not over-reject: a module that genuinely
    # defines its own `len` (shadowing the builtin) is bound and found.
    module = compile_starlark_module("def len(row):\n    return row\n", {})
    assert find_bound_function(module, ("len",)) == "len"


def test_a_name_bound_to_a_non_function_returns_none():
    module = compile_starlark_module("transform = 5\n", {})
    assert find_bound_function(module, ("transform",)) is None


def test_a_name_bound_to_type_range_returns_none():
    module = compile_starlark_module("transform = range(5)\n", {})
    assert find_bound_function(module, ("transform",)) is None


def test_a_list_holding_a_function_is_not_itself_a_function():
    # Regression: serde::serialize walks the whole value graph, so a name bound
    # to a LIST that merely CONTAINS a function raises the identical
    # "not supported on type `function`" message __getitem__ raises for a name
    # genuinely bound to a function. String-matching on that message alone
    # cannot tell the two apart — the ownership check (__getitem__) only
    # answers "is something unmarshallable bound here", never "is that
    # something itself a function". A second, TYPE-only check settles it.
    module = compile_starlark_module(
        "def f(row):\n    return row\ntransform = [f]\n", {}
    )
    assert find_bound_function(module, ("transform",)) is None


def test_a_dict_holding_a_function_is_not_itself_a_function():
    module = compile_starlark_module(
        "def f(row):\n    return row\ntransform = {'f': f}\n", {}
    )
    assert find_bound_function(module, ("transform",)) is None


def test_a_lambda_bound_to_the_name_is_accepted_as_a_function():
    # Deliberate: a lambda IS a function (same `type() == "function"`, same
    # callability as a `def`), so binding one to the wanted name is accepted,
    # not merely tolerated by accident.
    module = compile_starlark_module("transform = lambda row: row\n", {})
    assert find_bound_function(module, ("transform",)) == "transform"


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


def test_a_module_rebinding_type_itself_raises_rather_than_misreporting():
    # Known, accepted edge case: the TYPE half of the check (see
    # _is_bound_function) evaluates `type(<name>)` against the real module, so
    # a module that ALSO rebinds `type` itself (to something uncallable) breaks
    # that probe expression for every OTHER name, not just `type`. This is
    # deliberately left to propagate as a StarlarkError — fail loud on a bizarre
    # adversarial pattern — rather than attempting to special-case it into a
    # silent (and possibly wrong) True/False.
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
