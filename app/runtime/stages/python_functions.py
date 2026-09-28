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
from ..errors import AuthoredFrameExpected, MissingLineage
from app.models import WorkflowStage
from app.models.stages.code import (
    PythonFrameFunctionStage,
    PythonRowFunctionStage,
)
from app.models.stages.report import ReportStage

from ..branches import BranchRecorder
from ..code import load_function
from ..context import RunContext
from ..lineage import LineageRecorder
from ..stage_output import StageOutput
from .execution import RecordingRowMapper, Row, RowMapper, narrow_stage


# The three types whose behaviour is a `function` block.
CodeCarryingStage = PythonRowFunctionStage | PythonFrameFunctionStage | ReportStage

# Keyword-only, because every positional slot is an input frame.
LINEAGE_KWARG = "lineage"


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
    _require_lineage_keyword(fn, workflow_stage)
    # Pass dataframes positionally in declared input order.
    args = [table_to_frame(inputs[ref.id]) for ref in workflow_stage.inputs]
    recorder = LineageRecorder(inputs)
    kwargs: dict[str, object] = {LINEAGE_KWARG: recorder}
    if _declares_keyword_only(fn, "progress"):
        kwargs["progress"] = ctx.stage_progress
    frame = _require_frame(fn(*args, **kwargs), workflow_stage)
    return StageOutput.from_frame(frame, lineage=recorder.require_every_row(len(frame)))


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


def _require_lineage_keyword(fn: Callable[..., Any], workflow_stage: WorkflowStage) -> None:
    if _declares_keyword_only(fn, LINEAGE_KWARG):
        return
    input_id = workflow_stage.inputs[0].id
    raise MissingLineage(
        f"stage {workflow_stage.id}: a python_frame_function builds its own rows, so it "
        f"must say which input rows each one came from, and `{fn.__name__}` takes no "
        f"keyword-only `{LINEAGE_KWARG}`. Declare it: `{_render_def_with_lineage(fn)}`. "
        f"Then, for every row of the frame it returns, call "
        f"`{LINEAGE_KWARG}.built_from(row, \"{input_id}\", input_row)` for the input row "
        f"it was built from, `{LINEAGE_KWARG}.contributed_by(row, \"{input_id}\", "
        f"input_row, columns=[...])` for another input row that fed it, or "
        f"`{LINEAGE_KWARG}.originates(row)` where no input row did. Rows count from 0 in "
        f"the frames the function receives."
    )


def _render_def_with_lineage(fn: Callable[..., Any]) -> str:
    signature = inspect.signature(fn)
    kept = [p for p in signature.parameters.values() if p.name != LINEAGE_KWARG]
    recorder = inspect.Parameter(LINEAGE_KWARG, inspect.Parameter.KEYWORD_ONLY)
    # Parameter kinds sort into the order a signature must hold them in.
    ordered = sorted([*kept, recorder], key=lambda parameter: parameter.kind)
    return f"def {fn.__name__}{signature.replace(parameters=ordered)}"


def _declares_keyword_only(fn: Callable[..., Any], name: str) -> bool:
    try:
        declared = inspect.signature(fn).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return declared is not None and declared.kind is inspect.Parameter.KEYWORD_ONLY
