"""Runs a stage's python. The one place a code string is exec'd and the only
place that decides what names that code can see, so the scope it runs in is
settled here rather than per stage type."""
from __future__ import annotations

from typing import Any, Callable

from app.core.branch_source import RECORDER_NAME, instrument_branches
from app.models.errors import StepRefused

from .branches import BranchRecorder


def load_function(
    code: str, function_name: str, default_name: str,
    recorder: BranchRecorder | None = None,
) -> Callable[..., Any] | None:
    # Seeded so stage code can `raise StepRefused(...)` with no import line.
    namespace: dict[str, Any] = {StepRefused.__name__: StepRefused}
    if recorder is not None:
        code, _ = instrument_branches(code)
        namespace[RECORDER_NAME] = recorder.record
    exec(code, namespace)
    return namespace.get(function_name) or namespace.get(default_name)
