"""Handler for the filter_rows stage type: keeps the rows an authored
`should_include(row) -> bool` predicate returns True for, in input order,
with every column unchanged."""
from __future__ import annotations

import importlib
from typing import Any, Callable

import pandas as pd

from app.core.frames import list_rows
from app.models import FunctionKind, Stage

from ..context import RunContext
from .lineage import attach_row_provenance


def _load_predicate(stage: Stage) -> Callable[[dict[str, Any]], object]:
    cfg = stage.filter
    assert cfg is not None  # Stage validation: filter_rows always carries filter
    fn_name = cfg.function or "should_include"
    if cfg.kind == FunctionKind.module:
        if not cfg.module:
            raise ValueError(f"stage {stage.id}: filter.kind=module without module")
        module = importlib.import_module(cfg.module)
        return getattr(module, fn_name)
    if cfg.kind == FunctionKind.inline:
        ns: dict[str, Any] = {}
        exec(cfg.code or "", ns)
        fn = ns.get(fn_name) or ns.get("should_include")
        if fn is None:
            raise ValueError(f"inline 'should_include' not defined for stage {stage.id}")
        return fn
    raise ValueError(f"Unknown function kind for stage {stage.id}: {cfg.kind}")


def handle_filter_rows(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    predicate = _load_predicate(stage)
    src = inputs[stage.inputs[0].id]
    kept_positions = _find_kept_positions(stage, predicate, src)
    kept = src.iloc[kept_positions].reset_index(drop=True)
    return attach_row_provenance(
        kept, [stage.inputs[0].id] * len(kept_positions), kept_positions
    )


def _find_kept_positions(
    stage: Stage, predicate: Callable[[dict[str, Any]], object], src: pd.DataFrame
) -> list[int]:
    kept: list[int] = []
    for index, row in enumerate(list_rows(src)):
        result = predicate(row)
        if not isinstance(result, bool):
            raise ValueError(
                f"stage {stage.id}: should_include must return bool, got "
                f"{type(result).__name__} for row {index}"
            )
        if result:
            kept.append(index)
    return kept
