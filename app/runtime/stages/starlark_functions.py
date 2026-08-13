"""Handler for the starlark_row_function stage type: the sandboxed counterpart
of python_row_function, one compiled Starlark function mapped over the input's
rows through Task 1's marshalling boundary."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa

from app.core.starlark_source import DEFAULT_FUNCTION_NAME
from app.models import WorkflowStage
from app.models.stages.starlark import StarlarkRowFunctionStage

from ..starlark_code import compile_starlark_function
from .execution import Row, RowMapper, narrow_stage
from .starlark_marshal import marshal_row_for_starlark

if TYPE_CHECKING:
    # Type-only: `ctx` is part of MakeRowMapper's shape but this mapper reads
    # nothing off it, so the import stays out of the runtime graph.
    from ..context import RunContext


def make_starlark_row_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
) -> RowMapper:
    """Compile once; the mapper sees one marshalled row and nothing else."""
    starlark_stage = narrow_stage(workflow_stage, StarlarkRowFunctionStage)
    block = starlark_stage.starlark
    sid = starlark_stage.id
    function_name = block.function or DEFAULT_FUNCTION_NAME
    handle = compile_starlark_function(block.code, function_name, DEFAULT_FUNCTION_NAME)
    if handle is None:
        raise ValueError(
            f"starlark_row_function stage {sid}: code does not define "
            f"`{function_name}`"
        )

    def map_row(row: Row, index: int) -> Row:
        result = handle(marshal_row_for_starlark(row))
        if not isinstance(result, dict):
            raise ValueError(
                f"starlark_row_function stage {sid}: function must return a dict "
                f"per row, got {type(result).__name__}"
            )
        return result

    return map_row
