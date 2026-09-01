"""Runs a stage's Starlark. The one place the interpreter is driven, so what the
authored code can see — and how it refuses a row — is settled here."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

import starlark

from app.core.starlark_source import REFUSE_BUILTIN, compile_starlark_module, find_bound_function
from app.core.branch_source import RECORDER_NAME, instrument_branches
from app.models.errors import StepRefused

from .branches import BranchRecorder

# REFUSE_BUILTIN (imported above) is the builtin an author calls to refuse a
# row. Injected as a Python callable that raises StepRefused; the Rust frames
# discard the exception object and render its CLASS NAME into the message,
# which is what _find_refusal_message matches.

# starlark-pyo3 renders an injected callable's exception as
# "error: <ClassName>: <message>" on its own line. Built from the class so the
# sentinel can never drift from the type it stands for. An author cannot forge it:
# fail("StepRefused: x") renders as "error: fail: StepRefused: x".
_REFUSAL_MARKER = f"\nerror: {StepRefused.__name__}: "

# The span decoration starlark-pyo3 appends after a message; always last, so the
# author's own text is everything before the LAST occurrence.
_SPAN_MARKER = "\n --> "

# starlark-pyo3 renders exactly one "error: " line per failure, always the FIRST
# one in the text — the traceback frames above it never start a line this way.
# An author's own multi-line message (via fail(), or via row data threaded
# through fail()) can contain this text too, but only ever LATER in the string,
# which is why matching "first occurrence" rather than "anywhere" tells refusal
# apart from a forged look-alike.
_ERROR_LINE_MARKER = "\nerror: "


class StarlarkFunctionHandle:
    def __init__(self, frozen: starlark.FrozenModule, function_name: str) -> None:
        self._frozen = frozen
        self._function_name = function_name

    def __call__(self, *args: Any) -> object:
        try:
            return self._frozen.call(self._function_name, *args)
        except starlark.StarlarkError as exc:
            reason = _find_refusal_message(str(exc))
            if reason is None:
                raise
            raise StepRefused(reason) from exc


def compile_starlark_function(
    source: str, function_name: str, default_name: str,
    recorder: BranchRecorder | None = None,
    extra_builtins: Mapping[str, Callable[..., object]] | None = None,
) -> StarlarkFunctionHandle | None:
    builtins: dict[str, Callable[..., object]] = {REFUSE_BUILTIN: _refuse}
    builtins.update(extra_builtins or {})
    if recorder is not None:
        source, _ = instrument_branches(source)
        builtins[RECORDER_NAME] = recorder.record
    module = compile_starlark_module(source, builtins)
    bound = find_bound_function(module, (function_name, default_name))
    if bound is None:
        return None
    return StarlarkFunctionHandle(module.freeze(), bound)


def _refuse(reason: str) -> None:
    raise StepRefused(reason)


def _find_refusal_message(text: str) -> str | None:
    first_error_line = text.find(_ERROR_LINE_MARKER)
    if first_error_line == -1 or not text.startswith(_REFUSAL_MARKER, first_error_line):
        return None
    rest = text[first_error_line + len(_REFUSAL_MARKER):]
    end = rest.rfind(_SPAN_MARKER)
    return rest if end == -1 else rest[:end]
