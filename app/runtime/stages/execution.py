"""Handler shapes: what the runtime hands each stage type, and the row driver.

A stage type's grain-and-order guarantee is a fact about HOW the runtime invokes
its handler, not a claim the handler makes about itself. Each shape is a class
whose `execute` fixes the calling convention:

  RowMapHandler  — the runtime maps a per-row function over the single input's
                   rows and reassembles results in input order: one dict in, one
                   dict out. The mapper never sees the frame, so it cannot
                   reorder, drop, or fan out rows — preservation holds by
                   construction (issue #87).
  SourceHandler  — no upstream inputs; the handler originates rows from outside
                   the run. Trivially preserving: the rows begin here.
  FrameHandler   — the handler sees whole input frame(s) and may reshape or
                   reorder them freely; never grain-and-order preserving.

Preservation is carried by the shape CLASS — RowMap/Source preserve, Frame does
not — so a handler cannot separately declare itself preserving; it either is a
row-driven shape or it is not. Which shape a type registers under must agree with
the core fact (app.models is_grain_and_order_preserving); validate_registry_matches_model
holds the two equal when the registry module is imported.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import pandas as pd

from app.models import Stage
from app.models.stage import StageType, is_grain_and_order_preserving

from ..cancellation import consume_cancel
from ..errors import RunCancelled

# One row of a stage's input or output: column label → cell value.
Row = dict[str, Any]

# Sentinel column a row mapper attaches to a row it could not produce (e.g. an
# llm_transform whose generation failed). The row driver collects these off the
# assembled frame so the runner can surface them as error-severity output issues
# — a failed row is a reported error, not a silently dropped column.
ROW_ERROR_KEY = "_error"


class StageHandler(ABC):
    """One stage type's calling convention. `execute` runs the stage; the concrete
    shape fixes what the runtime hands the handler (a row at a time, nothing, or
    whole frames), which is what makes grain-and-order preservation structural."""

    @abstractmethod
    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]
    ) -> pd.DataFrame | None: ...


class RowMapHandler(StageHandler):
    """Driven per row by the runtime; the mapper never sees the frame, so it
    cannot reorder, drop, or fan out rows.

    `make_mapper` runs once per stage execution (resolve code, render prompt
    additions, record backend info) and returns the per-row function.
    `parallelism` > 1 lets the driver run the mapper over rows concurrently —
    results are written back by input index, so output order is input order
    regardless of completion order. `project_output_to_declared` asks the driver
    to project the assembled frame onto exactly the columns output_schema declares
    — a column-only operation that cannot change row count or order.
    """

    def __init__(
        self,
        make_mapper: Callable[[Stage, dict[str, Any]], Callable[[Row], Row]],
        parallelism: int = 1,
        project_output_to_declared: bool = False,
    ) -> None:
        self.make_mapper = make_mapper
        self.parallelism = parallelism
        self.project_output_to_declared = project_output_to_declared

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]
    ) -> pd.DataFrame:
        return _run_row_mapper(self, stage, inputs, ctx)


class SourceHandler(StageHandler):
    """Originates rows from outside the run; takes no upstream frames."""

    def __init__(self, read: Callable[[Stage, dict[str, Any]], pd.DataFrame]) -> None:
        self.read = read

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]
    ) -> pd.DataFrame:
        return self.read(stage, ctx)


class FrameHandler(StageHandler):
    """Sees whole input frame(s) keyed by upstream id; may reshape them."""

    def __init__(
        self,
        apply: Callable[[Stage, dict[str, pd.DataFrame], dict[str, Any]], pd.DataFrame | None],
    ) -> None:
        self.apply = apply

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]
    ) -> pd.DataFrame | None:
        return self.apply(stage, inputs, ctx)


# The shapes that guarantee row-by-row preservation — the runtime side of the
# core is_grain_and_order_preserving fact.
_PRESERVING_SHAPES = (RowMapHandler, SourceHandler)


def validate_registry_matches_model(handlers: dict[StageType, StageHandler]) -> None:
    """Raise unless each stage type's registered shape agrees with the core
    is_grain_and_order_preserving fact. Called when the registry module is
    imported, so a mis-shaped registration — a preserving type wired as a
    FrameHandler, or the reverse — cannot start the app."""
    for stage_type, handler in handlers.items():
        shape_preserves = isinstance(handler, _PRESERVING_SHAPES)
        if shape_preserves != is_grain_and_order_preserving(stage_type):
            raise RuntimeError(
                f"stage type {stage_type.value!r} is registered as "
                f"{type(handler).__name__} (preserving={shape_preserves}), but the "
                f"model declares grain-and-order-preserving="
                f"{is_grain_and_order_preserving(stage_type)}"
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

    results: list[Row | None] = [None] * len(records)
    if handler.parallelism > 1 and len(records) > 1:
        with ThreadPoolExecutor(max_workers=handler.parallelism) as pool:
            futures = {
                pool.submit(map_row, record): index
                for index, record in enumerate(records)
            }
            for future in as_completed(futures):
                if _consume_cancel(ctx):
                    # Drop every row not yet started; rows already dispatched
                    # (<= parallelism) keep running in their worker threads —
                    # a blocking call can't be killed — and are joined by the
                    # `with` block's own shutdown(wait=True) on the way out.
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise RunCancelled(f"stage {stage.id}: cancelled mid-fan-out")
                results[futures[future]] = future.result()
    else:
        for index, record in enumerate(records):
            if _consume_cancel(ctx):
                raise RunCancelled(f"stage {stage.id}: cancelled")
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
    _collect_row_errors(df, stage, ctx)
    if handler.project_output_to_declared:
        df = _project_onto_declared_columns(df, stage, ctx)
    return df


def _consume_cancel(ctx: dict[str, Any]) -> bool:
    """Consume this run's cancel message if one is pending — read-once, so a
    True means one was pending and is now gone. Identity is (project, run_id)
    read off ctx; False when either is absent: a subset/eval run's ctx carries
    neither key (see runner._subset_ctx), so those runs are never cancellable."""
    project, run_id = ctx.get("project"), ctx.get("run_id")
    if not isinstance(project, str) or not isinstance(run_id, str):
        return False
    return consume_cancel(project, run_id)


def _collect_row_errors(df: pd.DataFrame, stage: Stage, ctx: dict[str, Any]) -> None:
    """Record EVERY row carrying the `ROW_ERROR_KEY` sentinel, keyed by stage id on
    ctx. `pd.isna` alone is the test: it distinguishes a successful row (NaN — the
    mapper never set the sentinel) from a failed row (any string, including the
    empty string a message-less exception stringifies to). The runner surfaces
    these as error-severity output issues and marks the stage `error`; the stage
    keeps EVERY row (a failed row simply carries null/missing generated columns),
    so one failed row does not abort the stage."""
    if ROW_ERROR_KEY not in df.columns:
        return
    errors = [
        {"row": position, "message": str(value)}
        for position, value in enumerate(df[ROW_ERROR_KEY])
        if not pd.isna(value)
    ]
    if errors:
        ctx.setdefault("row_errors", {})[stage.id] = errors


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
