"""Runs a stage's Starlark. The one place the interpreter is driven, so what the
authored code can see — and how it refuses a row — is settled here."""
from __future__ import annotations

from typing import Any

import starlark

from app.core.starlark_source import compile_starlark_module, find_bound_function
from app.models.errors import StepRefused

# The builtin an author calls to refuse a row. Injected as a Python callable that
# raises StepRefused; the Rust frames discard the exception object and render its
# CLASS NAME into the message, which is what _find_refusal_message matches.
REFUSE_BUILTIN = "refuse"

# starlark-pyo3 renders an injected callable's exception as
# "error: <ClassName>: <message>" on its own line. Derived from the class so the
# sentinel can never drift from the type it stands for. An author cannot forge it:
# fail("StepRefused: x") renders as "error: fail: StepRefused: x".
_REFUSAL_MARKER = f"\nerror: {StepRefused.__name__}: "

# The span decoration starlark-pyo3 appends after a message; always last, so the
# author's own text is everything before the LAST occurrence.
_SPAN_MARKER = "\n --> "


class StarlarkFunctionHandle:
    """One compiled Starlark function, callable per row."""

    def __init__(self, frozen: starlark.FrozenModule, function_name: str) -> None:
        self._frozen = frozen
        self._function_name = function_name

    def __call__(self, row: dict[str, Any]) -> object:
        try:
            return self._frozen.call(self._function_name, row)
        except starlark.StarlarkError as exc:
            reason = _find_refusal_message(str(exc))
            if reason is None:
                raise
            raise StepRefused(reason) from exc


def compile_starlark_function(
    source: str, function_name: str, default_name: str
) -> StarlarkFunctionHandle | None:
    """None when neither name is bound to a function — the caller names what it wanted."""
    module = compile_starlark_module(source, {REFUSE_BUILTIN: _refuse})
    bound = find_bound_function(module, (function_name, default_name))
    if bound is None:
        return None
    return StarlarkFunctionHandle(module.freeze(), bound)


def _refuse(reason: str) -> None:
    raise StepRefused(reason)


def _find_refusal_message(text: str) -> str | None:
    start = text.find(_REFUSAL_MARKER)
    if start == -1:
        return None
    rest = text[start + len(_REFUSAL_MARKER):]
    end = rest.rfind(_SPAN_MARKER)
    return rest if end == -1 else rest[:end]
