"""Handler shapes: what the runtime hands each stage type, and the row driver.

A stage type's grain-and-order guarantee follows from HOW the runtime invokes its
handler, not from anything the handler's own body chooses to do. Each shape is a
class whose `execute` fixes the calling convention:

  RowMapHandler  — the runtime maps a per-row function over the single input's
                   rows and reassembles results in input order: one dict in, one
                   dict out. The mapper never sees the frame, so it cannot
                   reorder or fan out rows — that much holds by construction
                   (issue #87). Removing a row is possible only where the
                   handler declares `drops_rows`; the mapper marks the row and
                   the driver removes it.
  SourceHandler  — no upstream inputs; the handler originates rows from outside
                   the run. Trivially preserving: the rows begin here.
  FrameHandler   — the handler sees whole input frame(s) and may reshape or
                   reorder them freely; never grain-and-order preserving.

Preservation is reported by each handler's `preserves_grain_and_order` — Source
yes, Frame no, RowMap yes unless it declares `drops_rows`. It lives on the
handler rather than the shape class because row removal is the one thing a
row-driven shape can opt into, and a handler may only WEAKEN what its shape
would otherwise guarantee, never claim more than it. Which handler a type
registers under must agree with the core fact (app.models
is_grain_and_order_preserving); validate_registry_matches_model holds the two
equal when the registry module is imported.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import pandas as pd

from app.models import Stage
from app.models.stage import StageType, is_grain_and_order_preserving

from app.core.agent.usage import LlmUsage

from ..cancellation import consume_cancel
from ..context import RunContext
from ..manifest import CONTRIBUTION_ATTR, RowError, StageContribution
from ..errors import RunCancelled

# One row of a stage's input or output: column label → cell value.
Row = dict[str, Any]

# Sentinel column a row mapper attaches to a row it could not produce (e.g. an
# llm_transform whose generation failed). The row driver collects these off the
# assembled frame so the runner can surface them as error-severity output issues
# — a failed row is a reported error, not a silently dropped column.
ROW_ERROR_KEY = "_error"

# Sentinel column carrying a row's token/cost usage dict (an llm_transform
# attaches one per row). It is summed onto the stage's StageContribution and the
# column is then stripped, so usage never reaches stage output. The row driver
# does both for the stage it maps; a handler that assembles its own frame instead
# of being row-driven (llm_transform's batched path) does both for itself.
ROW_USAGE_KEY = "_usage"

# Sentinel column a row mapper attaches to a row whose value could not be
# produced synchronously: the value does not exist yet, so the run cannot be
# carried past this stage. Distinct from ROW_ERROR_KEY, which marks a row that
# FAILED and lets the run continue. The driver never interprets it — a handler
# that emits it reads it back in its own `collect_row_markers`.
ROW_DEFERRED_KEY = "_deferred"

# Sentinel column a row mapper sets to True on a row the stage does not emit. A
# mapper sees one row at a time and so cannot remove a row itself; it marks the
# row and the driver removes it — only where the handler declares `drops_rows`,
# since removing rows forfeits grain-and-order preservation.
ROW_DROP_KEY = "_drop"

# Internal per-row sentinel columns a mapper may attach. They are machinery, not
# stage output: the driver strips them off every mapped frame and does NOT
# report them as dropped user columns (they were collected by the driver or the
# handler's own collector, not discarded).
_INTERNAL_ROW_COLUMNS = frozenset(
    {ROW_ERROR_KEY, ROW_USAGE_KEY, ROW_DEFERRED_KEY, ROW_DROP_KEY}
)


class StageHandler(ABC):
    """One stage type's calling convention. `execute` runs the stage; the concrete
    shape fixes what the runtime hands the handler (a row at a time, nothing, or
    whole frames), which is what makes grain-and-order preservation structural."""

    @abstractmethod
    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame | None: ...

    @property
    @abstractmethod
    def preserves_grain_and_order(self) -> bool:
        """Does this handler guarantee that output row i came from input row i —
        1:1 and in the same order? Compared against the core
        is_grain_and_order_preserving fact for the stage type this handler is
        registered under (validate_registry_matches_model)."""


class RowMapHandler(StageHandler):
    """Driven per row by the runtime; the mapper never sees the frame, so it
    cannot reorder or fan out rows.

    `make_mapper` runs once per stage execution (resolve code, render prompt
    additions, record backend info) and returns the per-row function.
    `parallelism` > 1 lets the driver run the mapper over rows concurrently —
    results are written back by input index, so output order is input order
    regardless of completion order. `project_output_to_declared` asks the driver
    to project the assembled frame onto exactly the columns output_schema declares
    — a column-only operation that cannot change row count or order.

    `drops_rows` declares that this handler's mapper may mark a row with
    ROW_DROP_KEY for the driver to remove; the handler then stops claiming
    grain-and-order preservation. Left False, a drop marker is an error rather
    than a silent row loss. `collect_row_markers`, when given, runs once after
    the map with the assembled frame — every marker column still on it — so the
    handler can read back whatever its mapper attached before the driver strips
    the markers off, and report what it found onto the stage's
    `StageContribution`. It runs AFTER marked rows are removed, so it sees the
    surviving rows only; a handler needing a dropped row's markers must capture
    them in its mapper.
    """

    def __init__(
        self,
        make_mapper: Callable[[Stage, RunContext], Callable[[Row], Row]],
        parallelism: int = 1,
        project_output_to_declared: bool = False,
        drops_rows: bool = False,
        collect_row_markers: (
            Callable[[Stage, pd.DataFrame, RunContext, StageContribution], None] | None
        ) = None,
    ) -> None:
        self.make_mapper = make_mapper
        self.parallelism = parallelism
        self.project_output_to_declared = project_output_to_declared
        self.drops_rows = drops_rows
        self.collect_row_markers = collect_row_markers

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame:
        return _run_row_mapper(self, stage, inputs, ctx)

    @property
    def preserves_grain_and_order(self) -> bool:
        return not self.drops_rows


class LLMTransformHandler(RowMapHandler):
    """llm_transform's handler. `batch_size` picks between two SEPARATE execution
    functions — never a mode folded into one — because they differ in more than
    speed:

    - batch_size == 1 → `_run_row_mapper` (the inherited per-row path): grain,
      order, AND per-row independence hold by construction — the mapper never
      sees the frame.
    - batch_size  > 1 → `run_batches`: N rows per call, rejoined by a runtime-
      assigned batch row number. Grain and order still hold (one pre-allocated
      slot per input row, filled by index, assembled in order — and `run_batches`
      VERIFIES this before returning). Per-row INDEPENDENCE does not: the model
      sees a whole chunk in one prompt, so a row's answer can be influenced by
      its batch-mates.

    Subclassing RowMapHandler keeps `preserves_grain_and_order` honest for the
    property the registry invariant is about — grain and order, which BOTH paths
    keep. It deliberately does not claim per-row independence; batch_size>1 trades
    that for cost, which is why it is opt-in and defaults to 1.
    """

    def __init__(
        self,
        make_mapper: Callable[[Stage, RunContext], Callable[[Row], Row]],
        run_batches: Callable[[Stage, dict[str, pd.DataFrame], RunContext, int], pd.DataFrame],
        parallelism: int = 1,
        project_output_to_declared: bool = False,
    ) -> None:
        super().__init__(make_mapper, parallelism, project_output_to_declared)
        self.run_batches = run_batches

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame:
        assert stage.llm is not None  # Stage validation: an llm_transform always carries llm
        if stage.llm.batch_size > 1:
            return self.run_batches(stage, inputs, ctx, self.parallelism)
        return _run_row_mapper(self, stage, inputs, ctx)


class SourceHandler(StageHandler):
    """Originates rows from outside the run; takes no upstream frames."""

    def __init__(self, read: Callable[[Stage, RunContext], pd.DataFrame]) -> None:
        self.read = read

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame:
        return self.read(stage, ctx)

    @property
    def preserves_grain_and_order(self) -> bool:
        return True


class FrameHandler(StageHandler):
    """Sees whole input frame(s) keyed by upstream id; may reshape them."""

    def __init__(
        self,
        apply: Callable[[Stage, dict[str, pd.DataFrame], RunContext], pd.DataFrame | None],
    ) -> None:
        self.apply = apply

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame | None:
        return self.apply(stage, inputs, ctx)

    @property
    def preserves_grain_and_order(self) -> bool:
        return False


def validate_registry_matches_model(handlers: dict[StageType, StageHandler]) -> None:
    """Raise unless each registered handler's `preserves_grain_and_order` agrees
    with the core is_grain_and_order_preserving fact for its stage type. Called
    when the registry module is imported, so a mis-shaped registration — a
    preserving type wired as a FrameHandler, or the reverse — cannot start the
    app."""
    for stage_type, handler in handlers.items():
        handler_preserves = handler.preserves_grain_and_order
        if handler_preserves != is_grain_and_order_preserving(stage_type):
            raise RuntimeError(
                f"stage type {stage_type.value!r} is registered as "
                f"{type(handler).__name__} (preserving={handler_preserves}), but the "
                f"model declares grain-and-order-preserving="
                f"{is_grain_and_order_preserving(stage_type)}"
            )


def _run_row_mapper(
    handler: RowMapHandler,
    stage: Stage,
    inputs: dict[str, pd.DataFrame],
    ctx: RunContext,
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
    return _finish_mapped_frame(pd.DataFrame(out_rows), handler, stage, ctx)


def _finish_mapped_frame(
    df: pd.DataFrame, handler: RowMapHandler, stage: Stage, ctx: RunContext
) -> pd.DataFrame:
    """Turn the assembled per-row results into the stage's output frame: remove
    marked rows, collect the driver's own markers onto this stage's
    `StageContribution`, hand the frame to the handler's marker collector if it
    has one, then strip every marker column and — where the handler asks for it
    — project onto the declared columns.

    The collector's window is exact: it runs after the marked rows are already
    gone, so it sees the SURVIVING rows only, and before the strip, so it is the
    last step that sees a marker column at all.

    The contribution rides out on the returned frame's `.attrs`; the executor
    merges it into the manifest. Nothing accumulates in the (frozen) context."""
    contribution = StageContribution()
    df = _drop_marked_rows(df, handler, stage)
    _collect_row_errors(df, contribution)
    _collect_row_usage(df, contribution)
    if handler.collect_row_markers is not None:
        handler.collect_row_markers(stage, df, ctx, contribution)
    df = _strip_internal_row_columns(df)
    if handler.project_output_to_declared:
        df = _project_onto_declared_columns(df, stage, contribution)
    df.attrs[CONTRIBUTION_ATTR] = contribution
    return df


def _drop_marked_rows(
    df: pd.DataFrame, handler: RowMapHandler, stage: Stage
) -> pd.DataFrame:
    """Remove every row whose ROW_DROP_KEY marker is exactly True, re-indexing
    from 0 so downstream row positions stay contiguous. A frame with no marker
    column is returned unchanged. Raises unless the handler declares
    `drops_rows`: a marker from one that does not is a mapper bug, and acting on
    it would silently break the preservation the handler still claims."""
    if ROW_DROP_KEY not in df.columns:
        return df
    if not handler.drops_rows:
        raise ValueError(
            f"stage {stage.id}: a row carries the {ROW_DROP_KEY!r} marker, but this "
            f"handler does not declare row dropping"
        )
    _validate_drop_markers(df, stage)
    keep = pd.Series([value is not True for value in df[ROW_DROP_KEY]], index=df.index, dtype=bool)
    return df[keep].reset_index(drop=True)


def _validate_drop_markers(df: pd.DataFrame, stage: Stage) -> None:
    """Raise unless every ROW_DROP_KEY value is a plain `bool` or null.

    The removal decision is `value is True`, so a truthy STAND-IN — numpy's bool,
    an int, a string — would quietly KEEP a row the mapper meant to remove, which
    is data loss in the silent direction. Null stays legal: a mapper that marks
    only some rows leaves the rest missing, and the frame fills those with NaN."""
    for position, value in enumerate(df[ROW_DROP_KEY]):
        if not isinstance(value, bool) and not pd.isna(value):
            raise ValueError(
                f"stage {stage.id}: row {position} carries a {ROW_DROP_KEY!r} marker of type "
                f"{_name_type(value)}; it must be a plain bool or absent, because a row is "
                f"removed only on exactly True and any other value would silently keep it"
            )


def _name_type(value: object) -> str:
    """`value`'s type, module-qualified unless it is a builtin. numpy's bool
    reports the bare name "bool" exactly like the builtin does, so the bare name
    alone cannot tell an author which of the two they actually handed over."""
    cls = type(value)
    return cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"


def _strip_internal_row_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Drop every `_INTERNAL_ROW_COLUMNS` marker present on `df`. Unconditional
    — a marker is driver machinery, so it must never reach stage output, whether
    or not the stage declares an output_schema."""
    present = [column for column in df.columns if column in _INTERNAL_ROW_COLUMNS]
    return df.drop(columns=present) if present else df


def _consume_cancel(ctx: RunContext) -> bool:
    """Consume this run's cancel message if one is pending — read-once, so a
    True means one was pending and is now gone. False when `ctx.identity` is
    None: a subset/eval run's ctx carries no identity (see
    executor._subset_ctx), so those runs are never cancellable."""
    if ctx.identity is None:
        return False
    return consume_cancel(ctx.identity.project, ctx.identity.run_id)


def _collect_row_errors(df: pd.DataFrame, contribution: StageContribution) -> None:
    """Record EVERY row carrying the `ROW_ERROR_KEY` sentinel onto `contribution`.
    `pd.isna` alone is the test: it distinguishes a successful row (NaN — the
    mapper never set the sentinel) from a failed row (any string, including the
    empty string a message-less exception stringifies to). The runner surfaces
    these as error-severity output issues and marks the stage `error`; the stage
    keeps EVERY row (a failed row simply carries null/missing generated columns),
    so one failed row does not abort the stage."""
    if ROW_ERROR_KEY not in df.columns:
        return
    contribution.row_errors = [
        RowError(row=position, message=str(value))
        for position, value in enumerate(df[ROW_ERROR_KEY])
        if not pd.isna(value)
    ]


def _collect_row_usage(df: pd.DataFrame, contribution: StageContribution) -> None:
    """Sum every row's `ROW_USAGE_KEY` usage dict onto `contribution.llm_usage` —
    the stage's total token/cost spend. No column means a non-LLM stage (or a
    stage where usage was never reported): nothing is recorded, never a zero."""
    if ROW_USAGE_KEY not in df.columns:
        return
    parts = [value for value in df[ROW_USAGE_KEY] if isinstance(value, LlmUsage)]
    contribution.llm_usage = LlmUsage.summed(parts)


def _project_onto_declared_columns(
    df: pd.DataFrame, stage: Stage, contribution: StageContribution
) -> pd.DataFrame:
    """Project onto exactly the columns output_schema declares, in declared
    order. Column selection only — row count and order are untouched. Every
    column it drops is recorded on `contribution`, never silently discarded —
    and each is a user column, since the internal markers were already stripped
    off before this runs."""
    declared = [c.name for c in stage.output_schema.columns] if stage.output_schema else []
    if not declared:
        return df
    keep = [c for c in declared if c in df.columns]
    dropped = [str(c) for c in df.columns if c not in keep]
    if dropped:
        contribution.dropped_columns = dropped
    return df[keep]
