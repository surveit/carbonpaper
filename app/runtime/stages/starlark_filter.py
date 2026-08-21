"""Handler for the starlark_filter_rows stage type: the sandboxed counterpart of
filter_rows. Keeping a row is returning it and dropping one is returning None,
so which input ordinals survived is something the row driver knows without the
handler reporting it."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from app.models import WorkflowStage
from app.models.stages.starlark_filter import (
    DEFAULT_PREDICATE_NAME,
    StarlarkFilterRowsStage,
)

from ..branches import BranchRecorder
from ..starlark_code import compile_starlark_function
from .execution import RecordingRowMapper, Row, RowMapper, narrow_stage
from .starlark_marshal import marshal_row_for_starlark

if TYPE_CHECKING:
    from ..context import RunContext


def make_starlark_filter_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
) -> RowMapper:
    """Compile once, then decide one row at a time."""
    stage = narrow_stage(workflow_stage, StarlarkFilterRowsStage)
    block = stage.starlark_filter
    sid = stage.id
    function_name = block.function or DEFAULT_PREDICATE_NAME
    recorder = BranchRecorder()
    handle = compile_starlark_function(
        block.code, function_name, DEFAULT_PREDICATE_NAME, recorder)
    if handle is None:
        raise ValueError(
            f"starlark_filter_rows stage {sid}: code does not define `{function_name}`"
        )

    def keep_or_drop(row: Row, index: int) -> Row | None:
        result = handle(marshal_row_for_starlark(row))
        if not isinstance(result, bool):
            raise ValueError(
                f"starlark_filter_rows stage {sid}: {function_name} must return bool, "
                f"got {type(result).__name__} for row {index}"
            )
        return row if result else None

    return RecordingRowMapper(keep_or_drop, recorder)
