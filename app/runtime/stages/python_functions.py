"""Handlers for the python_row_function and python_frame_function stage types -
the two grains of running authored python over the input, differing only in
what the function is shown (a row dict or the whole frame).
"""

from __future__ import annotations

import importlib
from typing import Any, Callable

import pandas as pd

from app.core.errors import StepRefused
from app.models import FunctionKind, Stage
from app.models.stages.code import (
    PythonFrameFunctionStage,
    PythonRowFunctionStage,
)
from app.models.stages.publish import PublishStage

from ..context import RunContext
from .execution import Row, RowMapper, narrow_stage


# The three types whose behaviour is an authored `function` block.
CodeCarryingStage = PythonRowFunctionStage | PythonFrameFunctionStage | PublishStage


def _load_python_function(stage: CodeCarryingStage) -> Callable[..., Any]:
    """Resolve the callable for a stage carrying a function: block."""
    fn_spec = stage.function
    fn_name = fn_spec.function or "transform"
    if fn_spec.kind == FunctionKind.module:
        if not fn_spec.module:
            raise ValueError(f"stage {stage.id}: function.kind=module without module")
        module = importlib.import_module(fn_spec.module)
        return getattr(module, fn_name)
    if fn_spec.kind == FunctionKind.inline:
        # StepRefused is seeded so authored code can `raise StepRefused(...)` with
        # no import line — the refusal is meant to be the cheapest thing to write.
        ns: dict[str, Any] = {"StepRefused": StepRefused}
        exec(fn_spec.code or "", ns)
        fn = ns.get(fn_name) or ns.get("transform")
        if fn is None:
            raise ValueError(f"Inline function 'transform' not defined for stage {stage.id}")
        return fn
    raise ValueError(f"Unknown function kind for stage {stage.id}: {fn_spec.kind}")


def handle_python_frame_function(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Whole-frame transform: the function sees the full input frame(s) and may
    reshape them (group-by, pivot, dedup, multi-input merge)."""
    fn = _load_python_function(narrow_stage(stage, PythonFrameFunctionStage))
    # Pass dataframes positionally in declared input order.
    args = [inputs[ref.id] for ref in stage.inputs]
    return fn(*args)


def make_python_row_mapper(stage: Stage, ctx: RunContext, src: pd.DataFrame) -> RowMapper:
    """Resolve the stage's function once; the runtime maps it over the single
    input's rows — one dict in, one dict out. The authored function is shown
    neither the frame nor a row's position in it, so it cannot fan out, fan in,
    or reorder."""
    fn = _load_python_function(narrow_stage(stage, PythonRowFunctionStage))

    def map_row(row: Row, index: int) -> Row:
        result = fn(row)
        if not isinstance(result, dict):
            raise ValueError(
                f"python_row_function stage {stage.id}: function must return a dict "
                f"per row, got {type(result).__name__}"
            )
        return result

    return map_row
