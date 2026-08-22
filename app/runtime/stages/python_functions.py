"""Handlers for the python_row_function and python_frame_function stage types -
the two grains of running python over the input, differing only in
what the function is shown (a row dict or the whole frame).
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Callable

import pandas as pd
import pyarrow as pa

from app.core.frames import table_to_frame
from ..errors import AuthoredFrameExpected
from app.models import FunctionKind, WorkflowStage
from app.models.stages.code import (
    PythonFrameFunctionStage,
    PythonRowFunctionStage,
)
from app.models.stages.publish import PublishStage

from ..branches import BranchRecorder
from ..code import load_function
from ..context import RunContext
from ..lineage import LINEAGE_KWARG, LineageRecorder
from ..stage_output import StageOutput
from .execution import RecordingRowMapper, Row, RowMapper, narrow_stage


# The three types whose behaviour is a `function` block.
CodeCarryingStage = PythonRowFunctionStage | PythonFrameFunctionStage | PublishStage


def _load_python_function(
    stage: CodeCarryingStage, recorder: BranchRecorder | None = None
) -> Callable[..., Any]:
    fn_spec = stage.function
    fn_name = fn_spec.function or "transform"
    if fn_spec.kind == FunctionKind.module:
        if not fn_spec.module:
            raise ValueError(f"stage {stage.id}: function.kind=module without module")
        module = importlib.import_module(fn_spec.module)
        return getattr(module, fn_name)
    if fn_spec.kind == FunctionKind.inline:
        fn = load_function(fn_spec.code or "", fn_name, "transform", recorder)
        if fn is None:
            raise ValueError(f"Inline function 'transform' not defined for stage {stage.id}")
        return fn
    raise ValueError(f"Unknown function kind for stage {stage.id}: {fn_spec.kind}")


def handle_python_frame_function(
    workflow_stage: WorkflowStage, inputs: dict[str, pa.Table], ctx: RunContext
) -> StageOutput:
    """Whole-frame transform: the function may reshape (group-by, pivot, dedup, merge)."""
    fn = _load_python_function(narrow_stage(workflow_stage, PythonFrameFunctionStage))
    # Pass dataframes positionally in declared input order.
    args = [table_to_frame(inputs[ref.id]) for ref in workflow_stage.inputs]
    # A keyword bundle for authored code, so its value type is the caller's, not ours.
    declared: dict[str, object] = {}
    if _accepts_progress(fn):
        declared["progress"] = ctx.stage_progress
    # The stage may reshape, so nothing out here knows which input row became which
    # output row. A function that declares `lineage` says so itself; one that does
    # not reports none, and the trace stops at this stage as it always has.
    recorder = LineageRecorder(inputs) if _accepts_lineage(fn) else None
    if recorder is not None:
        declared[LINEAGE_KWARG] = recorder
    frame = _require_frame(fn(*args, **declared), workflow_stage)
    return StageOutput.from_frame(
        frame, lineage=None if recorder is None else recorder.resolve(len(frame))
    )


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


def _accepts_lineage(fn: Callable[..., Any]) -> bool:
    return _declares_keyword_only(fn, LINEAGE_KWARG)


def _accepts_progress(fn: Callable[..., Any]) -> bool:
    return _declares_keyword_only(fn, "progress")


def _declares_keyword_only(fn: Callable[..., Any], name: str) -> bool:
    try:
        declared = inspect.signature(fn).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return declared is not None and declared.kind is inspect.Parameter.KEYWORD_ONLY
