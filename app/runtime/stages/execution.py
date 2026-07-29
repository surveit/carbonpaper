"""Handler shapes: what the runtime hands each stage type, and the row driver.

A stage type's grain-and-order guarantee follows from HOW the runtime invokes its
handler, not from the handler's body: RowMap and Source preserve (RowMap unless
registered `drops_rows`, which keeps order but not grain), Frame does not."""
from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, NamedTuple, Protocol, runtime_checkable

import pandas as pd

from app.models import Stage
from app.models.stage import StageType, is_grain_and_order_preserving

from app.core.agent.usage import LlmUsage
from app.core.frames import list_rows
from app.core.stage_cache import StageCache, compute_row_fingerprint

from .frame_caching import (
    find_cached_frame,
    note_skipped_caching,
    open_frame_caching,
    record_frame_output,
)
from ..cancellation import consume_cancel
from ..context import RunContext
from ..lineage import attach_row_lineage, kept_rows_lineage
from ..manifest import CONTRIBUTION_ATTR, RowError, StageContribution
from ..errors import RunCancelled
from ..run_log import RunLog, bind_row_sink, unbind_detail_sink
from .row_events import (
    emit_batched_row_outcomes,
    emit_batched_row_starts,
    emit_cached_row,
    emit_row_outcome,
    emit_row_raised,
    emit_row_start,
)

# One row of a stage's input or output: column label → cell value.
Row = dict[str, Any]

# One stage execution's per-row function: a row and that row's position in the
# input frame in, one row out — or None to drop the row, which only a mapper
# whose handler declares `drops_rows` may return. The position lets a mapper
# read its own entry out of something its factory worked out over the whole
# input; a mapper that needs nothing frame-wide ignores it.
RowMapper = Callable[[Row, int], "Row | None"]

# Builds the per-row function for ONE stage execution, from the stage, the run
# context, and the single input frame the map is about to run over. The frame is
# handed to the FACTORY, never to the mapper: work that is cheaper — or only
# correct — done once over the whole input happens here, and the mapper it
# returns still sees one row at a time.
MakeRowMapper = Callable[[Stage, RunContext, pd.DataFrame], RowMapper]

# An LLMTransformHandler's batched execution function: the stage's inputs, the
# run, the driver's parallelism, and the INPUT POSITION of each row it is handed
# (in the order handed), which is what lets it attribute a chunk's log detail to
# the rows it actually covers. It computes one raw row per input row it is
# given — internal columns still attached, nothing stripped or projected — and knows
# nothing about caching: which rows it is asked about is the shape's decision
# (see `_run_batched`).
RunBatches = Callable[
    [Stage, dict[str, pd.DataFrame], RunContext, int, list[int]], list["Row"]
]

# Internal column a row mapper attaches to a row it could not produce (e.g. an
# llm_transform whose generation failed). The row driver collects these off the
# assembled frame so the runner can surface them as error-severity output issues
# — a failed row is a reported error, not a silently dropped column.
ROW_ERROR_KEY = "_error"

# Internal column carrying a row's token/cost usage dict (an llm_transform
# attaches one per row). It is summed onto the stage's StageContribution and the
# column is then stripped, so usage never reaches stage output. The row driver
# does both for the stage it maps; a handler that assembles its own frame instead
# of being row-driven (llm_transform's batched path) does both for itself.
ROW_USAGE_KEY = "_usage"

# Internal column a row mapper attaches to a row whose value could not be
# produced synchronously: the value does not exist yet, so the run cannot be
# carried past this stage. Distinct from ROW_ERROR_KEY, which marks a row that
# FAILED and lets the run continue. The driver never interprets it — a mapper
# that emits it reads it back in its own `finish_mapped_rows`.
ROW_DEFERRED_KEY = "_deferred"


class _InternalRowColumn(NamedTuple):
    """One internal column a mapper may attach, and what the driver does about
    it. Both behaviors are stated per column so a new one cannot be given one
    and silently forgotten the other."""

    column: str
    # Machinery, not stage output: dropped off every mapped frame, and NOT
    # reported as a dropped user column (it was collected by the driver or read
    # back by the mapper's own post-map step, not discarded).
    stripped_from_output: bool
    # Marks a row that is not an output the stage produced, so the row must
    # never be pinned as its input key's answer.
    blocks_recording: bool


# The ONE declaration of the internal row columns: `_strip_internal_columns` and
# `_record_row_output` read the two behaviors off this table.
_INTERNAL_ROW_COLUMNS = (
    _InternalRowColumn(ROW_ERROR_KEY, stripped_from_output=True, blocks_recording=True),
    _InternalRowColumn(ROW_USAGE_KEY, stripped_from_output=True, blocks_recording=False),
    _InternalRowColumn(ROW_DEFERRED_KEY, stripped_from_output=True, blocks_recording=True),
)


@runtime_checkable
class PostMapRowMapper(Protocol):
    """A row mapper that also carries the step to run once its map is over.

    `make_mapper` may return a plain function — one row in, one row out — or an
    object of this shape, which additionally gets `finish_mapped_rows` once the
    assembled frame exists, with every internal column still on it. The two
    halves then share whatever per-execution state the object holds, instead of
    needing a channel outside the mapper to pass it through.

    `finish_mapped_rows` runs on the assembled frame — one row per input row —
    and before the driver strips the internal columns, so it is the last step
    that sees one at all. It is handed the stage's `StageContribution` to report
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

    `drops_rows` widens the mapper's return to `Row | None`, None meaning DROP
    THIS ROW — the one way a row-mapped stage may emit fewer rows than it was
    given (filter_rows). Order and 1-to-at-most-1 still hold by construction, so
    the driver knows exactly which input ordinal each surviving row came from
    and records it as this stage's lineage; grain no longer holds, which is why
    the property below reports it. `caches_rows=False` skips row-grain caching
    for a stage whose per-row compute is cheaper than the fingerprint a lookup
    would have to hash (the frame-level counterpart is FrameHandler's
    `caches_frames`).

    `make_mapper` runs once per stage execution (resolve code, render prompt
    additions, record backend info, work anything frame-wide out ahead of the
    map) and returns the per-row callable — a plain function, or a
    `PostMapRowMapper`, which the driver also hands the assembled frame once the
    map is over. `parallelism` > 1 lets the driver run the mapper over rows
    concurrently — results are written back by input index, so output order is
    input order regardless of completion order. `project_output_to_declared`
    asks the driver to project the assembled frame onto exactly the columns
    output_schema declares — a column-only operation that cannot change row
    count or order. A row-mapped stage resolves each row against the
    stage-result cache before calling the mapper (see `_open_row_caching`)
    unless it registers `caches_rows=False`.
    """

    def __init__(
        self,
        make_mapper: MakeRowMapper,
        parallelism: int = 1,
        project_output_to_declared: bool = False,
        drops_rows: bool = False,
        caches_rows: bool = True,
    ) -> None:
        self.make_mapper = make_mapper
        self.parallelism = parallelism
        self.project_output_to_declared = project_output_to_declared
        self.drops_rows = drops_rows
        self.caches_rows = caches_rows

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
        make_mapper: MakeRowMapper,
        run_batches: RunBatches,
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
            return _run_batched(self, stage, inputs, ctx)
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
    """Sees whole input frame(s) keyed by upstream id; may reshape them.

    `caches_frames` lets the runtime resolve the WHOLE output frame against the
    stage-result cache instead of calling `apply` (see
    `frame_caching.open_frame_caching`). Two kinds of registration pass it
    False: one whose stage is terminal and side-effecting — its output is read
    by the world, not by a later run — and one whose compute is cheaper than
    fingerprinting the input a lookup would have to hash.
    """

    def __init__(
        self,
        apply: Callable[[Stage, dict[str, pd.DataFrame], RunContext], pd.DataFrame | None],
        caches_frames: bool = True,
    ) -> None:
        self.apply = apply
        self.caches_frames = caches_frames

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame | None:
        caching = open_frame_caching(stage, ctx, self.caches_frames)
        if caching.key is None:
            output = self.apply(stage, inputs, ctx)
            return note_skipped_caching(output, caching.skipped_note)
        input_frames = [inputs[ref.id] for ref in stage.inputs]
        cached = find_cached_frame(caching, input_frames)
        if cached is not None:
            return cached
        return record_frame_output(caching, input_frames, self.apply(stage, inputs, ctx))

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
    0x0 frame.

    Under `handler.drops_rows` a slot may come back None, meaning the row is
    dropped; order still holds, and the assembly loop below keeps the input
    ordinals it emitted as the stage's lineage."""
    if len(stage.inputs) != 1:
        raise ValueError(
            f"stage {stage.id}: a row-mapped stage takes exactly one input, "
            f"got {len(stage.inputs)}"
        )
    src = inputs[stage.inputs[0].id]
    map_row = handler.make_mapper(stage, ctx, src)
    # The ONE line of per-row compute, optionally routed through the row cache
    # and the run log. Log outside cache, so a row the cache answers never
    # reaches the mapper's lifecycle wrapper and is logged as the replay it is.
    # `map_row` itself stays bound: _finish_mapped_frame tests it for the
    # PostMapRowMapper shape, which a wrapper would hide.
    caching = _open_row_caching(stage, ctx) if handler.caches_rows else None
    compute_row = _log_row_lifecycle(map_row, ctx.run_log, stage.id)
    if caching is not None:
        compute_row = _map_row_through_cache(caching, compute_row, ctx.run_log, stage.id)
    records = list_rows(src)

    results: list[Row | None] = [None] * len(records)
    if handler.parallelism > 1 and len(records) > 1:
        with ThreadPoolExecutor(max_workers=handler.parallelism) as pool:
            futures = {
                pool.submit(compute_row, record, index): index
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
            results[index] = compute_row(record, index)

    out_rows: list[Row] = []
    kept_indices: list[int] = []
    for index, result in enumerate(results):
        if result is None and handler.drops_rows:
            continue
        if not isinstance(result, dict):
            raise ValueError(
                f"stage {stage.id}: row mapper must return one dict per row, "
                f"got {type(result).__name__} for row {index}"
            )
        out_rows.append(result)
        kept_indices.append(index)
    mapped = _finish_mapped_frame(pd.DataFrame(out_rows), handler, map_row, stage, ctx)
    out = _restore_input_columns_when_nothing_named_them(mapped, src)
    if handler.drops_rows:
        # The driver, not the stage, knows which input ordinals survived.
        attach_row_lineage(out, kept_rows_lineage(stage.inputs[0].id, kept_indices))
    return out


# ── the row-level cache interceptor ──────────────────────────────────────────
# Caching is a property of the handler SHAPE, not of any stage type: the one
# line where per-row compute happens is wrapped, so every row-mapped stage type
# is cached by the same code and no stage implements a cache interface. The
# cache store and its keying live below the seam (app.core.stage_cache); what
# lives here is one execution's state over it and the two decisions the runtime
# owns: WHETHER caching applies at all, and whether a given result is one the
# stage actually produced and may therefore be recorded.


class _RowCaching(NamedTuple):
    """One row-mapped execution's row-grain cache state.

    `recorded_outputs` is read ONCE for the whole execution — a per-row store
    lookup would make a stage's store cost scale with its row count — and is
    empty where nothing may be replayed. `writer` is None under a read-only
    accessor, which reuses hits and records nothing."""

    project: str
    stage_id: str
    stage_fingerprint: str
    recorded_outputs: dict[str, Row]
    writer: StageCache | None


def _open_row_caching(stage: Stage, ctx: RunContext) -> _RowCaching | None:
    """None where caching does not apply: the stage declares `cache: false`
    (intentionally non-deterministic — always re-roll), or the run carries no
    project scope (a subset run, a preview, an authored-test run).

    Under `ctx.bust_cache` nothing already recorded is read, while what this run
    computes is still recorded — so a busted run ends with the cache re-pinned,
    not stale."""
    if not stage.cache:
        return None
    if ctx.identity is None or ctx.stage_cache is None:
        return None
    project = ctx.identity.project
    stage_fingerprint = stage.compute_definition_fingerprint()
    return _RowCaching(
        project,
        stage.id,
        stage_fingerprint,
        {} if ctx.bust_cache else ctx.stage_cache.find_recorded_rows(
            project, stage.id, stage_fingerprint
        ),
        ctx.stage_cache if isinstance(ctx.stage_cache, StageCache) else None,
    )


def _find_cached_row(caching: _RowCaching, input_row: Row) -> Row | None:
    recorded = caching.recorded_outputs.get(compute_row_fingerprint(input_row))
    return None if recorded is None else dict(recorded)


def _record_row_output(caching: _RowCaching, input_row: Row, output_row: Row) -> None:
    """Pin one computed row, unless the run cannot write or an internal column on
    the row says it is not an output the stage produced. No internal column is
    ever part of the recorded row, so a replayed row reports no spend."""
    if caching.writer is None:
        return
    if any(
        output_row.get(internal.column) is not None
        for internal in _INTERNAL_ROW_COLUMNS
        if internal.blocks_recording
    ):
        return
    caching.writer.record(
        project=caching.project,
        stage_id=caching.stage_id,
        stage_fingerprint=caching.stage_fingerprint,
        input_fingerprint=compute_row_fingerprint(input_row),
        input_row=input_row,
        output_row=_without_internal_columns(output_row),
    )


def _without_internal_columns(row: Row) -> Row:
    internal = {column.column for column in _INTERNAL_ROW_COLUMNS}
    return {key: value for key, value in row.items() if key not in internal}


def _map_row_through_cache(
    caching: _RowCaching, map_row: RowMapper, log: RunLog | None, stage_id: str
) -> RowMapper:
    def compute_row(row: Row, index: int) -> Row | None:
        cached = _find_cached_row(caching, row)
        if cached is not None:
            emit_cached_row(log, stage_id, index)
            return cached
        result = map_row(row, index)
        # A drop is not a recordable output: the store holds output ROWS, so a
        # replayed drop would be indistinguishable from a miss.
        if result is not None:
            _record_row_output(caching, row, result)
        return result

    return compute_row


# ── the run log's row lifecycle ──────────────────────────────────────────────


def _log_row_lifecycle(map_row: RowMapper, log: RunLog | None, stage_id: str) -> RowMapper:
    """`map_row` wrapped in its own row_start/row_ok/row_error events."""
    if log is None:
        return map_row

    def compute_row(row: Row, index: int) -> Row | None:
        emit_row_start(log, stage_id, index)
        # Bind the detail sink so the LLM layer, several frames below map_row,
        # can attribute its prompt/thinking/response to this (stage, row)
        # without any of that being threaded through the mapper's signature.
        token = bind_row_sink(log, stage_id, index)
        try:
            result = map_row(row, index)
        except Exception as exc:  # noqa: BLE001 — logged, then re-raised unchanged;
            # the executor's own per-stage handling is untouched.
            emit_row_raised(log, stage_id, index, exc)
            raise
        finally:
            unbind_detail_sink(token)
        # A dropped row (None) ran to completion — it has no error to report.
        emit_row_outcome(log, stage_id, index, result.get(ROW_ERROR_KEY) if result else None)
        return result

    return compute_row


# ── the batched llm_transform path ───────────────────────────────────────────
# The one row-mapped path the driver above does not run: rows are computed N per
# model call. Cache resolution is therefore spelled out here, in the SHAPE — the
# stage's own module is handed the rows to compute and knows nothing about the
# cache, the key, or what may be recorded.


def _run_batched(
    handler: "LLMTransformHandler",
    stage: Stage,
    inputs: dict[str, pd.DataFrame],
    ctx: RunContext,
) -> pd.DataFrame:
    """Run the stage's batched execution function over only the rows the cache
    cannot already answer, and assemble its rows back into INPUT order alongside
    the hits."""
    src = inputs[stage.inputs[0].id]
    records = list_rows(src)
    caching = _open_row_caching(stage, ctx)
    hits = {} if caching is None else _find_cached_rows_by_position(caching, records)
    misses = [index for index in range(len(records)) if index not in hits]
    emit_batched_row_starts(ctx.run_log, stage.id, hits, misses)
    computed = _compute_batched_rows(handler, stage, src, misses, ctx)
    # Ordered before recorded: the ordering step is what verifies one computed
    # row per miss, so nothing is pinned against a row it did not come from.
    rows = _order_by_input_position(stage, hits, misses, computed, len(records))
    if caching is not None:
        for position, row in zip(misses, computed):
            _record_row_output(caching, records[position], row)
    emit_batched_row_outcomes(
        ctx.run_log, stage.id, misses, [row.get(ROW_ERROR_KEY) for row in computed]
    )
    return _restore_input_columns_when_nothing_named_them(
        _finish_batched_frame(rows, handler, stage), src
    )


def _find_cached_rows_by_position(
    caching: _RowCaching, records: list[Row]
) -> dict[int, Row]:
    """Every input row the cache can already answer, by input position."""
    found: dict[int, Row] = {}
    for index, record in enumerate(records):
        cached = _find_cached_row(caching, record)
        if cached is not None:
            found[index] = cached
    return found


def _compute_batched_rows(
    handler: "LLMTransformHandler",
    stage: Stage,
    src: pd.DataFrame,
    misses: list[int],
    ctx: RunContext,
) -> list[Row]:
    """One raw output row per MISS, in miss order. No miss at all means the
    execution function is never called, so a fully-cached stage makes no model
    call."""
    if not misses:
        return []
    return handler.run_batches(
        stage, {stage.inputs[0].id: src.iloc[misses]}, ctx, handler.parallelism, misses
    )


def _order_by_input_position(
    stage: Stage,
    hits: dict[int, Row],
    misses: list[int],
    computed: list[Row],
    row_count: int,
) -> list[Row]:
    """One row per input row, in input order: the cache hit where there was one,
    the freshly computed row where there was not. Raises unless exactly one
    computed row came back per miss — a gap would silently mis-grain the
    stage."""
    if len(computed) != len(misses):
        raise RuntimeError(
            f"stage {stage.id}: batched execution returned {len(computed)} rows "
            f"for the {len(misses)} rows it was asked to compute"
        )
    by_position = dict(hits)
    by_position.update(zip(misses, computed))
    return [by_position[position] for position in range(row_count)]


def _finish_batched_frame(
    rows: list[Row], handler: RowMapHandler, stage: Stage
) -> pd.DataFrame:
    """The batched path's counterpart of `_finish_mapped_frame`: no mapper, so
    no post-map step — the internal columns are collected off the assembled
    frame, then stripped and the frame projected."""
    df = pd.DataFrame(rows)
    return _strip_and_project(df, _collect_internal_columns(df), handler, stage)


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
    the driver's own internal columns onto this stage's `StageContribution`, hand
    the frame back to the mapper where the mapper is a `PostMapRowMapper`, then
    strip every internal column and — where the handler asks for it — project
    onto the declared columns.

    The mapper's window is exact: `finish_mapped_rows` runs before the strip, so
    it is the last step that sees an internal column at all.

    The contribution rides out on the returned frame's `.attrs`; the executor
    merges it into the manifest. Nothing accumulates in the (frozen) context."""
    contribution = _collect_internal_columns(df)
    if isinstance(map_row, PostMapRowMapper):
        map_row.finish_mapped_rows(stage, df, ctx, contribution)
    return _strip_and_project(df, contribution, handler, stage)


def _collect_internal_columns(df: pd.DataFrame) -> StageContribution:
    """This stage's contribution, carrying what the driver reads off the internal
    columns of the assembled frame."""
    contribution = StageContribution()
    _collect_row_errors(df, contribution)
    _collect_row_usage(df, contribution)
    return contribution


def _strip_and_project(
    df: pd.DataFrame,
    contribution: StageContribution,
    handler: RowMapHandler,
    stage: Stage,
) -> pd.DataFrame:
    """`df` with every internal column dropped and — where the handler asks for
    it — projected onto the declared columns, carrying `contribution` out on its
    `.attrs` for the executor to merge into the manifest.

    Strip before project, in that order: the projection reports every column it
    drops as a user column the stage produced and discarded, and an internal
    column is driver machinery, not that."""
    df = _strip_internal_columns(df)
    if handler.project_output_to_declared:
        df = _project_onto_declared_columns(df, stage, contribution)
    df.attrs[CONTRIBUTION_ATTR] = contribution
    return df


def _strip_internal_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Drop every column `_INTERNAL_ROW_COLUMNS` declares strippable that is
    present on `df`. Unconditional — an internal column is driver machinery, so
    it must never reach stage output, whether or not the stage declares an
    output_schema."""
    stripped = {
        internal.column
        for internal in _INTERNAL_ROW_COLUMNS
        if internal.stripped_from_output
    }
    present = [column for column in df.columns if column in stripped]
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
    and each is a user column, since the internal columns were already stripped
    off before this runs. Raises when a declared column is absent, except on a
    frame whose rows already reported generation failures — a failed row
    produces no generated value, and its row errors fail the stage anyway."""
    declared = [c.name for c in stage.output_schema.columns] if stage.output_schema else []
    if not declared:
        return df
    if not len(df.columns) and not len(df):
        # Not a violation: with an empty input no mapper result named a single
        # column, so no row failed to produce a declared one. The driver hands
        # this frame the input's own columns
        # (_restore_input_columns_when_nothing_named_them).
        return df
    missing = [name for name in declared if name not in df.columns]
    if missing and not contribution.row_errors:
        raise ValueError(
            f"stage '{stage.id}' declares output column(s) {missing} that it did not "
            f"produce; the frame carries {[str(c) for c in df.columns]}. A declared "
            "column is what downstream stages are entitled to read — it can be neither "
            "invented nor dropped from the projection."
        )
    df = df.reindex(columns=[*df.columns, *missing]) if missing else df
    dropped = [str(c) for c in df.columns if c not in declared]
    if dropped:
        contribution.dropped_columns = dropped
    return df[declared]
