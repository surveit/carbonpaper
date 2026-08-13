"""Handler for the filter_rows stage type: a `should_include(row) ->
bool` predicate, mapped over rows by the runtime's row driver. Keeping a row is
returning it; dropping one is returning None — the driver does the selecting,
so which input ordinals survived (this stage's lineage) is something it knows
without any handler here saying so."""
from __future__ import annotations

from typing import Any, Callable

import pyarrow as pa

from app.models import WorkflowStage
from app.models.stages.filter_rows import FilterRowsStage

from ..code import load_function
from ..context import RunContext
from .execution import Row, RowMapper, narrow_stage


def _load_predicate(
    filter_stage: FilterRowsStage,
) -> Callable[[dict[str, Any]], object]:
    cfg = filter_stage.filter
    fn_name = cfg.function or "should_include"
    fn = load_function(cfg.code, fn_name, "should_include")
    if fn is None:
        raise ValueError(
            f"inline 'should_include' not defined for stage {filter_stage.id}")
    return fn


def make_filter_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
) -> RowMapper:
    """Resolve the predicate once, then decide one row at a time."""
    filter_stage = narrow_stage(workflow_stage, FilterRowsStage)
    predicate = _load_predicate(filter_stage)
    sid = filter_stage.id

    def keep_or_drop(row: Row, index: int) -> Row | None:
        result = predicate(row)
        if not isinstance(result, bool):
            raise ValueError(
                f"stage {sid}: should_include must return bool, got "
                f"{type(result).__name__} for row {index}"
            )
        return row if result else None

    return keep_or_drop
