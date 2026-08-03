"""Handler for the starlark_row_function stage type: the sandboxed counterpart
of python_row_function, one compiled Starlark function mapped over the input's
rows through Task 1's marshalling boundary."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from app.models import Stage
from app.models.stages.starlark import StarlarkRowFunctionStage

from ..starlark_code import compile_starlark_function
from .execution import Row, RowMapper, narrow_stage
from .starlark_marshal import marshal_row_for_starlark

if TYPE_CHECKING:
    # Type-only: `ctx` is part of MakeRowMapper's shape but this mapper reads
    # nothing off it, so the import stays out of the runtime graph.
    from ..context import RunContext

# Matches app.models.stages.starlark._DEFAULT_FUNCTION_NAME: the name execution
# falls back to when `function` is unset or the explicit empty string a saved
# stage may carry (see StarlarkFunction.function's write-time `wanted = function
# or default` idiom).
_DEFAULT_FUNCTION = "transform"


def make_starlark_row_mapper(stage: Stage, ctx: RunContext, src: pd.DataFrame) -> RowMapper:
    """Compile once; the mapper sees one marshalled row and nothing else."""
    block = narrow_stage(stage, StarlarkRowFunctionStage).starlark
    function_name = block.function or _DEFAULT_FUNCTION
    handle = compile_starlark_function(block.code, function_name, _DEFAULT_FUNCTION)
    if handle is None:
        raise ValueError(
            f"starlark_row_function stage {stage.id}: code does not define "
            f"`{function_name}`"
        )

    def map_row(row: Row, index: int) -> Row:
        result = handle(marshal_row_for_starlark(row))
        if not isinstance(result, dict):
            raise ValueError(
                f"starlark_row_function stage {stage.id}: function must return a dict "
                f"per row, got {type(result).__name__}"
            )
        return result

    return map_row
