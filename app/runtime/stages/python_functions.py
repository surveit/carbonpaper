"""Handlers for running authored python over the input, at two grains that differ
only in what the function is shown: a row dict (python_row_function) or the whole
frame (python_frame_function). The code is always the code the stage carries.
"""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from app.models import Stage
from app.models.stages.code import (
    PythonFrameFunctionStage,
    PythonRowFunctionStage,
)
from app.models.stages.publish import PublishStage

from ..code import load_function
from ..context import RunContext
from .execution import Row, RowMapper, narrow_stage


# The three types whose behaviour is a `function` block.
CodeCarryingStage = PythonRowFunctionStage | PythonFrameFunctionStage | PublishStage


def _load_python_function(stage: CodeCarryingStage) -> Callable[..., Any]:
    """The callable this stage runs, compiled from the code the stage carries."""
    fn_spec = stage.function
    fn = load_function(fn_spec.code, fn_spec.function or "transform", "transform")
    if fn is None:
        raise ValueError(f"Inline function 'transform' not defined for stage {stage.id}")
    return fn


def handle_python_frame_function(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Whole-frame transform: the function sees the full input frame(s) and may
    reshape them (group-by, pivot, dedup, multi-input merge)."""
    fn = _load_python_function(narrow_stage(stage, PythonFrameFunctionStage))
    # Pass dataframes positionally in declared input order.
    args = [inputs[ref.id] for ref in stage.inputs]
    return fn(*args)


def make_python_row_mapper(stage: Stage, ctx: RunContext, src: pd.DataFrame) -> RowMapper:
    """Resolve the stage's function once; the runtime maps it over the single
    input's rows — one dict in, one dict out. The function is shown
    neither the frame nor a row's position in it, so it cannot fan out, fan in,
    or reorder."""
    fn = _load_python_function(narrow_stage(stage, PythonRowFunctionStage))

    def map_row(row: Row, index: int) -> Row:
        result = fn(row)
        if not isinstance(result, dict):
            raise ValueError(
                f"{stage.type} stage {stage.id}: function must return a dict "
                f"per row, got {type(result).__name__}"
            )
        return result

    return map_row
