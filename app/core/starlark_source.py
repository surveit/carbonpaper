"""Loading Starlark source and asking what it bound. Loading EXECUTES the
source's top-level statements, not merely compiles them. The interpreter
wrapper, with no knowledge of stages: what a bound function MEANS is the
caller's business."""
from __future__ import annotations

from typing import Callable, Mapping, Sequence

import starlark

_FUNCTION_TYPE_NAME = "function"

# The function name validation falls back to, and execution falls back to in
# turn — one definition so the two layers cannot drift apart.
DEFAULT_FUNCTION_NAME = "transform"

# The builtin name an author calls to refuse a row, injected identically at
# write-time validation and at execution — one definition so the two layers
# cannot drift apart.
REFUSE_BUILTIN = "refuse"


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
    # A non-identifier could inject Starlark into `_bound_value_type`'s probe.
    if not name.isidentifier():
        raise ValueError(f"Not a valid Starlark identifier: {name!r}")
    # Two questions, two primitives: bound at all, then bound to a function.
    if not _module_binds(module, name):
        return False
    return _bound_value_type(module, name) == _FUNCTION_TYPE_NAME


def _module_binds(module: starlark.Module, name: str) -> bool:
    # Reads only what THIS module's own top-level statements bound.
    try:
        # Unlike evaluating `name` as an expression, `module[name]` never falls
        # back to a standard-library name (`len`, `fail`, ...) the code never
        # bound itself. It raises for any value it cannot marshal to Python (a
        # function, a `range`, a container HOLDING one); a bound plain value or
        # an unbound name both just return — indistinguishable here, and it
        # doesn't matter: neither is ever a function, all this needs to settle.
        module[name]
    except starlark.StarlarkError:
        return True
    return False


def _bound_value_type(module: starlark.Module, name: str) -> str:
    # Ownership is settled by `_module_binds`; this asks what the value IS.
    probe = starlark.parse("<probe>", f"type({name})")
    # `module[name]` raises identically for a function and for a container
    # holding one somewhere inside (serde walks the whole graph); `type(name)`
    # reports only the immediate value's type.
    return str(starlark.eval(module, probe, starlark.Globals.standard()))
