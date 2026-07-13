"""Handler shapes: what the runtime hands each stage type, and the row driver.

A stage type's grain-and-order guarantee is a fact about HOW the runtime
invokes its handler, not a claim the handler makes about itself:

  RowMapHandler  — the runtime maps a per-row function over the single input's
                   rows and reassembles results in input order: one dict in,
                   one dict out. The mapper never sees the frame, so it cannot
                   reorder, drop, or fan out rows — preservation holds by
                   construction (issue #87).
  SourceHandler  — no upstream inputs; the handler originates rows from outside
                   the run. Trivially preserving: the rows begin here.
  FrameHandler   — the handler sees whole input frame(s) and may reshape or
                   reorder them freely; never grain-and-order preserving.

The shape a type registers under is the runtime half of the model's
GRAIN_AND_ORDER_PRESERVING_TYPES declaration; `check_registry_matches_model`
holds the two sides equal when the registry module is imported.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, ClassVar

import pandas as pd

from app.models import Stage
from app.models.stage import GRAIN_AND_ORDER_PRESERVING_TYPES, StageType

# One row of a stage's input or output: column label → cell value.
Row = dict[str, Any]


@dataclass(frozen=True)
class RowMapHandler:
    """Driven per row by the runtime; the mapper never sees the frame.

    `make_mapper` runs once per stage execution (resolve code, render prompt
    additions, record backend info) and returns the per-row function.
    `parallelism` > 1 lets the driver run the mapper over rows concurrently —
    results are written back by input index, so output order is input order
    regardless of completion order. `project_output_to_declared` asks the
    driver to project the assembled frame onto exactly the columns
    output_schema declares — a column-only operation that cannot change row
    count or order.
    """
    make_mapper: Callable[[Stage, dict[str, Any]], Callable[[Row], Row]]
    parallelism: int = 1
    project_output_to_declared: bool = False
    is_grain_and_order_preserving: ClassVar[bool] = True


@dataclass(frozen=True)
class SourceHandler:
    """Originates rows from outside the run; takes no upstream frames."""
    read: Callable[[Stage, dict[str, Any]], pd.DataFrame]
    is_grain_and_order_preserving: ClassVar[bool] = True


@dataclass(frozen=True)
class FrameHandler:
    """Sees whole input frame(s) keyed by upstream id; may reshape them."""
    apply: Callable[[Stage, dict[str, pd.DataFrame], dict[str, Any]], pd.DataFrame | None]
    is_grain_and_order_preserving: ClassVar[bool] = False


StageHandler = RowMapHandler | SourceHandler | FrameHandler


def execute_handler(
    handler: StageHandler,
    stage: Stage,
    inputs: dict[str, pd.DataFrame],
    ctx: dict[str, Any],
) -> pd.DataFrame | None:
    """Run one stage through its shape's calling convention."""
    if isinstance(handler, SourceHandler):
        return handler.read(stage, ctx)
    if isinstance(handler, RowMapHandler):
        return _run_row_mapper(handler, stage, inputs, ctx)
    return handler.apply(stage, inputs, ctx)


def check_registry_matches_model(handlers: dict[StageType, StageHandler]) -> None:
    """Raise unless the registry's handler shapes agree with the model's
    GRAIN_AND_ORDER_PRESERVING_TYPES declaration. Called when the registry
    module is imported, so a mis-shaped registration cannot start the app."""
    by_shape = frozenset(
        stage_type for stage_type, handler in handlers.items()
        if handler.is_grain_and_order_preserving
    )
    if by_shape != GRAIN_AND_ORDER_PRESERVING_TYPES:
        raise RuntimeError(
            "handler registry disagrees with GRAIN_AND_ORDER_PRESERVING_TYPES: "
            f"registered shapes imply {sorted(t.value for t in by_shape)}, "
            f"the model declares "
            f"{sorted(t.value for t in GRAIN_AND_ORDER_PRESERVING_TYPES)}"
        )


def _run_row_mapper(
    handler: RowMapHandler,
    stage: Stage,
    inputs: dict[str, pd.DataFrame],
    ctx: dict[str, Any],
) -> pd.DataFrame:
    """Map the stage's per-row function over its single input, in input order.

    Grain and order hold by construction: exactly one result slot exists per
    input row, filled by input index (also under concurrency), and the output
    frame is assembled in index order."""
    if len(stage.inputs) != 1:
        raise ValueError(
            f"stage {stage.id}: a row-mapped stage takes exactly one input, "
            f"got {len(stage.inputs)}"
        )
    src = inputs[stage.inputs[0].id]
    map_row = handler.make_mapper(stage, ctx)
    # str(k) pins pandas' Hashable column labels down to str (a no-op for
    # parquet/CSV data, whose labels are already strings).
    records: list[Row] = [
        {str(k): v for k, v in record.items()} for record in src.to_dict("records")
    ]

    results: list[Any] = [None] * len(records)
    if handler.parallelism > 1 and len(records) > 1:
        with ThreadPoolExecutor(max_workers=handler.parallelism) as pool:
            futures = {
                pool.submit(map_row, record): index
                for index, record in enumerate(records)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
    else:
        for index, record in enumerate(records):
            results[index] = map_row(record)

    out_rows: list[Row] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(
                f"stage {stage.id}: row mapper must return one dict per row, "
                f"got {type(result).__name__} for row {index}"
            )
        out_rows.append(result)
    df = pd.DataFrame(out_rows)
    if handler.project_output_to_declared:
        df = _project_onto_declared_columns(df, stage, ctx)
    return df


def _project_onto_declared_columns(
    df: pd.DataFrame, stage: Stage, ctx: dict[str, Any]
) -> pd.DataFrame:
    """Project onto exactly the columns output_schema declares, in declared
    order. Column selection only — row count and order are untouched. Dropped
    columns are recorded on ctx, never silently discarded."""
    declared = [c.name for c in stage.output_schema.columns] if stage.output_schema else []
    if not declared:
        return df
    keep = [c for c in declared if c in df.columns]
    dropped = [str(c) for c in df.columns if c not in keep]
    if dropped:
        ctx.setdefault("dropped_columns", {})[stage.id] = dropped
    return df[keep]
