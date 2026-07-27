"""Handler shapes: what the runtime hands each stage type, and the row driver.

A stage type's grain-and-order guarantee follows from HOW the runtime invokes its
handler, not from anything the handler's own body chooses to do. Each shape is a
class whose `execute` fixes the calling convention:

  RowMapHandler  — the runtime maps a per-row function over the single input's
                   rows and reassembles results in input order: one dict in, one
                   dict out. The mapper never sees the frame — only the factory
                   that builds it does, before the map starts — so it cannot
                   reorder, fan out or remove rows: that much holds by
                   construction (issue #87).
  SourceHandler  — no upstream inputs; the handler originates rows from outside
                   the run. Trivially preserving: the rows begin here.
  FrameHandler   — the handler sees whole input frame(s) and may reshape or
                   reorder them freely; never grain-and-order preserving.

Preservation is reported by each handler's `preserves_grain_and_order` — Source
yes, Frame no, RowMap yes — and is fixed by the shape alone, not by anything an
individual handler declares. Which handler a type registers under must agree
with the core fact (app.models is_grain_and_order_preserving);
validate_registry_matches_model holds the two equal when the registry module is
imported.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Protocol, runtime_checkable

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

# One stage execution's per-row function: a row and that row's position in the
# input frame in, one row out. The position lets a mapper read its own entry out
# of something its factory worked out over the whole input; a mapper that needs
# nothing frame-wide ignores it.
RowMapper = Callable[[Row, int], Row]

# Builds the per-row function for ONE stage execution, from the stage, the run
# context, and the single input frame the map is about to run over. The frame is
# handed to the FACTORY, never to the mapper: work that is cheaper — or only
# correct — done once over the whole input happens here, and the mapper it
# returns still sees one row at a time.
MakeRowMapper = Callable[[Stage, RunContext, pd.DataFrame], RowMapper]

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
# FAILED and lets the run continue. The driver never interprets it — a mapper
# that emits it reads it back in its own `finish_mapped_rows`.
ROW_DEFERRED_KEY = "_deferred"

# Internal per-row sentinel columns a mapper may attach. They are machinery, not
# stage output: the driver strips them off every mapped frame and does NOT
# report them as dropped user columns (they were collected by the driver or read
# back by the mapper's own post-map step, not discarded).
_INTERNAL_ROW_COLUMNS = frozenset({ROW_ERROR_KEY, ROW_USAGE_KEY, ROW_DEFERRED_KEY})


@runtime_checkable
class PostMapRowMapper(Protocol):
    """A row mapper that also carries the step to run once its map is over.

    `make_mapper` may return a plain function — one row in, one row out — or an
    object of this shape, which additionally gets `finish_mapped_rows` once the
    assembled frame exists, with every marker column still on it. The two halves
    then share whatever per-execution state the object holds, instead of needing
    a channel outside the mapper to pass it through.

    `finish_mapped_rows` runs on the assembled frame — one row per input row —
    and before the driver strips the markers, so it is the last step that sees a
    marker column at all. It is handed the stage's `StageContribution` to report
    onto — the manifest fields the mapper owns — and may raise, which aborts the
    stage."""

    def __call__(self, row: Row, index: int) -> Row: ...

    def finish_mapped_rows(
        self,
        stage: Stage,
        df: pd.DataFrame,
        ctx: RunContext,
        contribution: StageContribution,
    ) -> None: ...


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
    additions, record backend info, work anything frame-wide out ahead of the
    map) and returns the per-row callable — a plain function, or a
    `PostMapRowMapper`, which the driver also hands the assembled frame once the
    map is over. `parallelism` > 1 lets the driver run the mapper over rows
    concurrently — results are written back by input index, so output order is
    input order regardless of completion order. `project_output_to_declared`
    asks the driver to project the assembled frame onto exactly the columns
    output_schema declares — a column-only operation that cannot change row
    count or order.
    """

    def __init__(
        self,
        make_mapper: MakeRowMapper,
        parallelism: int = 1,
        project_output_to_declared: bool = False,
    ) -> None:
        self.make_mapper = make_mapper
        self.parallelism = parallelism
        self.project_output_to_declared = project_output_to_declared

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame:
        return _run_row_mapper(self, stage, inputs, ctx)

    @property
    def preserves_grain_and_order(self) -> bool:
        return True


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
        make_mapper: MakeRowMapper,
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
    frame is assembled in index order. A result with no rows AND no columns —
    an empty input — takes the input's columns instead of being handed on as a
    0x0 frame."""
    if len(stage.inputs) != 1:
        raise ValueError(
            f"stage {stage.id}: a row-mapped stage takes exactly one input, "
            f"got {len(stage.inputs)}"
        )
    src = inputs[stage.inputs[0].id]
    map_row = handler.make_mapper(stage, ctx, src)
    # str(k) pins pandas' Hashable column labels down to str (a no-op for
    # parquet/CSV data, whose labels are already strings).
    records: list[Row] = [
        {str(k): v for k, v in record.items()} for record in src.to_dict("records")
    ]

    results: list[Row | None] = [None] * len(records)
    if handler.parallelism > 1 and len(records) > 1:
        with ThreadPoolExecutor(max_workers=handler.parallelism) as pool:
            futures = {
                pool.submit(map_row, record, index): index
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
            results[index] = map_row(record, index)

    out_rows: list[Row] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(
                f"stage {stage.id}: row mapper must return one dict per row, "
                f"got {type(result).__name__} for row {index}"
            )
        out_rows.append(result)
    mapped = _finish_mapped_frame(pd.DataFrame(out_rows), handler, map_row, stage, ctx)
    return _restore_input_columns_when_nothing_named_them(mapped, src)


def _restore_input_columns_when_nothing_named_them(
    mapped: pd.DataFrame, src: pd.DataFrame
) -> pd.DataFrame:
    """The mapped frame, or — when it has neither rows nor columns — an empty
    slice of the stage's input, carrying the mapped frame's `.attrs`.

    A frame assembled from no results at all is 0 rows BY 0 COLUMNS: the input
    was empty, so no mapper result named a single column. A downstream stage
    keyed on an upstream column would then raise `KeyError` instead of producing
    an empty result, and the input's own columns are the one honest shape
    available. A frame that still carries a row or a column is returned
    untouched.

    The substituted frame takes the mapped one's `.attrs` verbatim, because the
    stage's StageContribution rides there: an empty-input stage still reported
    whatever it reported onto that contribution, and swapping the frame must not
    swallow it."""
    if len(mapped.columns) > 0 or len(mapped) > 0:
        return mapped
    empty = src.iloc[0:0].copy()
    empty.attrs = dict(mapped.attrs)
    return empty


def _finish_mapped_frame(
    df: pd.DataFrame,
    handler: RowMapHandler,
    map_row: RowMapper,
    stage: Stage,
    ctx: RunContext,
) -> pd.DataFrame:
    """Turn the assembled per-row results into the stage's output frame: collect
    the driver's own markers onto this stage's `StageContribution`, hand the
    frame back to the mapper where the mapper is a `PostMapRowMapper`, then
    strip every marker column and — where the handler asks for it — project onto
    the declared columns.

    The mapper's window is exact: `finish_mapped_rows` runs before the strip, so
    it is the last step that sees a marker column at all.

    The contribution rides out on the returned frame's `.attrs`; the executor
    merges it into the manifest. Nothing accumulates in the (frozen) context."""
    contribution = StageContribution()
    _collect_row_errors(df, contribution)
    _collect_row_usage(df, contribution)
    if isinstance(map_row, PostMapRowMapper):
        map_row.finish_mapped_rows(stage, df, ctx, contribution)
    df = _strip_internal_row_columns(df)
    if handler.project_output_to_declared:
        df = _project_onto_declared_columns(df, stage, contribution)
    df.attrs[CONTRIBUTION_ATTR] = contribution
    return df


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
