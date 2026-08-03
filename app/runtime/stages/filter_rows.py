"""Handler for the filter_rows stage type: an authored `should_include(row) ->
bool` predicate, mapped over rows by the runtime's row driver. Keeping a row is
returning it; dropping one is returning None — the driver does the selecting,
so which input ordinals survived (this stage's lineage) is something it knows
without any handler here saying so."""
from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from app.models import Stage
from app.models.errors import StepRefused
from app.models.stages.filter_rows import FilterRowsStage

from ..context import RunContext
from .execution import Row, RowMapper, narrow_stage


def _load_predicate(stage: Stage) -> Callable[[dict[str, Any]], object]:
    cfg = narrow_stage(stage, FilterRowsStage).filter
    fn_name = cfg.function or "should_include"
    # StepRefused is seeded so the predicate can `raise StepRefused(...)` with no
    # import line — a row it cannot decide must be cheaper to refuse than to guess.
    ns: dict[str, Any] = {StepRefused.__name__: StepRefused}
    exec(cfg.code, ns)
    fn = ns.get(fn_name) or ns.get("should_include")
    if fn is None:
        raise ValueError(f"inline 'should_include' not defined for stage {stage.id}")
    return fn


def make_filter_mapper(stage: Stage, ctx: RunContext, src: pd.DataFrame) -> RowMapper:
    """Resolve the predicate once, then decide one row at a time."""
    predicate = _load_predicate(stage)

    def keep_or_drop(row: Row, index: int) -> Row | None:
        result = predicate(row)
        if not isinstance(result, bool):
            raise ValueError(
                f"stage {stage.id}: should_include must return bool, got "
                f"{type(result).__name__} for row {index}"
            )
        return row if result else None

    return keep_or_drop
