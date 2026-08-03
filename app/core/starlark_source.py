"""Compiling Starlark source and asking what it bound. The interpreter wrapper,
with no knowledge of stages: what a bound function MEANS is the caller's business."""
from __future__ import annotations

from typing import Callable, Mapping, Sequence

import starlark

_FUNCTION_TYPE_NAME = "function"
_UNBOUND_TEMPLATE = "Variable `{name}` not found"


def compile_starlark_module(
    source: str, builtins: Mapping[str, Callable[..., object]]
) -> starlark.Module:
    """Evaluate `source` with `builtins` injected as callables. Raises StarlarkError."""
    module = starlark.Module()
    for name, builtin in builtins.items():
        module.add_callable(name, builtin)
    starlark.eval(module, starlark.parse("<stage>", source), starlark.Globals.standard())
    return module


def find_bound_function(module: starlark.Module, names: Sequence[str]) -> str | None:
    """The first of `names` bound to a function, or None."""
    return next((name for name in names if _is_bound_function(module, name)), None)


def _is_bound_function(module: starlark.Module, name: str) -> bool:
    # Module has no `get`; indexing an absent name returns None and indexing a
    # FUNCTION raises. Evaluating `type(name)` as a top-level EXPRESSION is the
    # only probe that answers this — as a statement, eval returns None instead.
    probe = starlark.parse("<probe>", f"type({name})")
    try:
        return starlark.eval(module, probe, starlark.Globals.standard()) == _FUNCTION_TYPE_NAME
    except starlark.StarlarkError as exc:
        if _UNBOUND_TEMPLATE.format(name=name) in str(exc):
            return False
        raise
