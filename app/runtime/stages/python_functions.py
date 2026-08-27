"""Handlers for the python_row_function and python_frame_function stage types -
the two grains of running python over the input, differing only in
what the function is shown (a row dict or the whole frame).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

import pandas as pd
import pyarrow as pa

from app.core.frames import table_to_frame
from ..errors import AuthoredFrameExpected
from app.models import WorkflowStage
from app.models.stages.code import (
    PythonFrameFunctionStage,
    PythonRowFunctionStage,
)
from app.models.stages.report import ReportStage

from ..branches import BranchRecorder
from ..code import load_function
from ..context import RunContext
from ..stage_output import StageOutput
from .execution import RecordingRowMapper, Row, RowMapper, narrow_stage


# The three types whose behaviour is a `function` block.
CodeCarryingStage = PythonRowFunctionStage | PythonFrameFunctionStage | ReportStage


def _load_python_function(
    stage: CodeCarryingStage, recorder: BranchRecorder | None = None
) -> Callable[..., Any]:
    fn_spec = stage.function
    fn_name = fn_spec.function or "transform"
    fn = load_function(fn_spec.code, fn_name, "transform", recorder)
    if fn is None:
        raise ValueError(f"Inline function 'transform' not defined for stage {stage.id}")
    return fn


def handle_python_frame_function(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], ctx: RunContext
) -> StageOutput:
    """Whole-frame transform: the function may reshape (group-by, pivot, dedup, merge)."""
    fn = _load_python_function(narrow_stage(workflow_stage, PythonFrameFunctionStage))
    # Pass dataframes positionally in declared input order.
    args = [table_to_frame(inputs[ref.id]) for ref in workflow_stage.inputs]
    kwargs = {"progress": ctx.stage_progress} if _accepts_progress(fn) else {}
    return StageOutput.from_frame(_require_frame(fn(*args, **kwargs), workflow_stage))


def build_python_row_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
) -> RowMapper:
    """One dict in, one dict out: shown neither the frame nor a row's position in it."""
    stage = narrow_stage(workflow_stage, PythonRowFunctionStage)
    recorder = BranchRecorder()
    fn = _load_python_function(stage, recorder)

    def map_row(row: Row, index: int) -> Row:
        result = fn(row)
        if not isinstance(result, dict):
            raise ValueError(
                f"python_row_function stage {stage.id}: function must return a dict "
                f"per row, got {type(result).__name__}"
            )
        return result

    return RecordingRowMapper(map_row, recorder)


def _require_frame(result: Any, workflow_stage: WorkflowStage) -> pd.DataFrame:
    """Checked before the coercion to arrow, so a wrong return type is not reported as a crash."""
    if not isinstance(result, pd.DataFrame):
        raise AuthoredFrameExpected(
            f"stage {workflow_stage.id}: function returned {type(result).__name__}, "
            f"expected a DataFrame",
            type(result).__name__,
        )
    return result


def _accepts_progress(fn: Callable[..., Any]) -> bool:
    try:
        progress = inspect.signature(fn).parameters.get("progress")
    except (TypeError, ValueError):
        return False
    return progress is not None and progress.kind is inspect.Parameter.KEYWORD_ONLY
