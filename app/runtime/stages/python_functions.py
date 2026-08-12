"""Handlers for the python_row_function and python_frame_function stage types -
the two grains of running python over the input, differing only in
what the function is shown (a row dict or the whole frame).
"""

from __future__ import annotations

import importlib
from typing import Any, Callable

import pandas as pd

from app.models import FunctionKind, WorkflowStage
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
    fn_spec = stage.function
    fn_name = fn_spec.function or "transform"
    if fn_spec.kind == FunctionKind.module:
        if not fn_spec.module:
            raise ValueError(f"stage {stage.id}: function.kind=module without module")
        module = importlib.import_module(fn_spec.module)
        return getattr(module, fn_name)
    if fn_spec.kind == FunctionKind.inline:
        fn = load_function(fn_spec.code or "", fn_name, "transform")
        if fn is None:
            raise ValueError(f"Inline function 'transform' not defined for stage {stage.id}")
        return fn
    raise ValueError(f"Unknown function kind for stage {stage.id}: {fn_spec.kind}")


def handle_python_frame_function(
    workflow_stage: WorkflowStage, inputs: dict[str, pd.DataFrame], ctx: RunContext
) -> pd.DataFrame:
    fn = _load_python_function(narrow_stage(workflow_stage, PythonFrameFunctionStage))
    # Pass dataframes positionally in declared input order.
    args = [inputs[ref.id] for ref in workflow_stage.inputs]
    return fn(*args)


def make_python_row_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pd.DataFrame
) -> RowMapper:
    stage = narrow_stage(workflow_stage, PythonRowFunctionStage)
    fn = _load_python_function(stage)

    def map_row(row: Row, index: int) -> Row:
        result = fn(row)
        if not isinstance(result, dict):
            raise ValueError(
                f"python_row_function stage {stage.id}: function must return a dict "
                f"per row, got {type(result).__name__}"
            )
        return result

    return map_row
