"""Loading Starlark source and asking what it bound. Loading EXECUTES the
source's top-level statements, not merely compiles them. The interpreter
wrapper, with no knowledge of stages: what a bound function MEANS is the
caller's business."""
from __future__ import annotations

from typing import Callable, Mapping, Sequence

import starlark

_UNSERIALIZABLE_FUNCTION_MARKER = "not supported on type `function`"

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
    # Not interpolated into Starlark source (see the `__getitem__` probe below),
    # but still validated: a malformed name is a config error, not "unbound".
    if not name.isidentifier():
        raise ValueError(f"Not a valid Starlark identifier: {name!r}")
    # `module[name]` reads only what THIS module's own top-level statements
    # bound — unlike evaluating `name` as a Starlark expression, it never falls
    # back to a standard-library name (`len`, `dict`, `fail`, ...) that the code
    # never bound itself, and it still sees a name that genuinely shadows one.
    # A bound function is the one value this binding can't marshal to Python,
    # so failing to marshal it for exactly that reason is itself the signal.
    try:
        module[name]
    except starlark.StarlarkError as exc:
        return _UNSERIALIZABLE_FUNCTION_MARKER in str(exc)
    return False
