"""Handler shapes: what the runtime hands each stage type, and the row driver.

A stage type's grain-and-order guarantee follows from HOW the runtime invokes its
handler, not from the handler's body: RowMap and Source preserve (RowMap unless
registered `drops_rows`, which keeps order but not grain), Frame does not."""
from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, NamedTuple, Protocol, TypeVar, runtime_checkable

import pandas as pd

from app.models import Stage
from app.models.run_manifest import RowError, StageContribution
from app.models.stage import (
    AbstractStage,
    StageType,
    is_grain_and_order_preserving,
    max_declared_inputs,
)
from app.models.stages.llm_transform import LLMTransformStage

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
from ..manifest import CONTRIBUTION_ATTR
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

_StageT = TypeVar("_StageT", bound=AbstractStage)


def narrow_stage(stage: Stage, model: type[_StageT]) -> _StageT:
    if isinstance(stage, model):
        return stage
    raise TypeError(
        f"stage {stage.id}: this handler runs a {model.__name__}, "
        f"got a {type(stage).__name__}"
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
# given — internal columns still attached, nothing stripped or trimmed — and knows
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

# Internal column marking a row the row cache answered rather than the stage
# computing it. Stamped where the hit is read, so both row-mapped paths carry it;
# the driver counts them onto the stage's StageContribution. A computed row never
# carries the column, so a stage with no hits reports no count rather than a zero.
ROW_CACHED_KEY = "_cached"


class _InternalRowColumn(NamedTuple):
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
    _InternalRowColumn(ROW_CACHED_KEY, stripped_from_output=True, blocks_recording=False),
)


@runtime_checkable
class PostMapRowMapper(Protocol):
    """`finish_mapped_rows` runs before the driver strips the internal columns, and may abort."""

    def __call__(self, row: Row, index: int) -> Row: ...

    def finish_mapped_rows(
        self,
        stage: Stage,
        df: pd.DataFrame,
        ctx: RunContext,
        contribution: StageContribution,
    ) -> None: ...


class StageHandler(ABC):
    @abstractmethod
    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame | None: ...

    @property
    @abstractmethod
    def preserves_grain_and_order(self) -> bool: ...


class RowMapTransformHandler(StageHandler):
    def __init__(
        self,
        make_mapper: MakeRowMapper,
        parallelism: int = 1,
        trims_output_to_declared: bool = False,
        drops_rows: bool = False,
        caches_rows: bool = True,
    ) -> None:
        self.make_mapper = make_mapper
        self.parallelism = parallelism
        self.trims_output_to_declared = trims_output_to_declared
        self.drops_rows = drops_rows
        self.caches_rows = caches_rows

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame:
        return _run_row_mapper(self, stage, inputs, ctx)

    @property
    def preserves_grain_and_order(self) -> bool:
        return not self.drops_rows


class LLMTransformHandler(RowMapTransformHandler):
    """batch_size > 1 keeps grain and order but NOT per-row independence: the model sees the chunk."""

    def __init__(
        self,
        make_mapper: MakeRowMapper,
        run_batches: RunBatches,
        parallelism: int = 1,
        trims_output_to_declared: bool = False,
    ) -> None:
        super().__init__(make_mapper, parallelism, trims_output_to_declared)
        self.run_batches = run_batches

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame:
        if narrow_stage(stage, LLMTransformStage).llm.batch_size > 1:
            return _run_batched(self, stage, inputs, ctx)
        return _run_row_mapper(self, stage, inputs, ctx)


class SourceHandler(StageHandler):
    def __init__(self, read: Callable[[Stage, RunContext], pd.DataFrame]) -> None:
        self.read = read

    def execute(
        self, stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext
    ) -> pd.DataFrame:
        return self.read(stage, ctx)

    @property
    def preserves_grain_and_order(self) -> bool:
        return True


class FrameTransformHandler(StageHandler):
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
    for stage_type, handler in handlers.items():
        handler_preserves = handler.preserves_grain_and_order
        if handler_preserves != is_grain_and_order_preserving(stage_type):
            raise RuntimeError(
                f"stage type {stage_type.value!r} is registered as "
                f"{type(handler).__name__} (preserving={handler_preserves}), but the "
                f"model declares grain-and-order-preserving="
                f"{is_grain_and_order_preserving(stage_type)}"
            )
        # The arity rule lives on the model's `inputs` field and is checked here
        # rather than per execution: a row-mapped handler maps ONE frame's rows,
        # so a type that admits a second input names no rows to map.
        if isinstance(handler, RowMapTransformHandler) and max_declared_inputs(stage_type) != 1:
            raise RuntimeError(
                f"stage type {stage_type.value!r} is registered as "
                f"{type(handler).__name__}, which maps one frame's rows, but its "
                f"model caps `inputs` at {max_declared_inputs(stage_type)} — declare "
                f"max_length=1"
            )


def _run_row_mapper(
    handler: RowMapTransformHandler,
    stage: Stage,
    inputs: dict[str, pd.DataFrame],
    ctx: RunContext,
) -> pd.DataFrame:
    src = inputs[stage.inputs[0].id]
    reads = stage.anchor_reads()
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
    seen = [_narrow_row(row, reads) for row in records]

    results: list[Row | None] = [None] * len(records)
    if handler.parallelism > 1 and len(records) > 1:
        with ThreadPoolExecutor(max_workers=handler.parallelism) as pool:
            futures = {
                pool.submit(compute_row, record, index): index
                for index, record in enumerate(seen)
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
        for index, record in enumerate(seen):
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
        # Rejoin: under narrowing the mapper only ever saw its declared reads, so
        # the columns that merely FLOW come back from the input row here. The
        # mapper's own keys win — that is what a rewrite or an add is.
        out_rows.append({**records[index], **result})
        kept_indices.append(index)
    mapped = _finish_mapped_frame(pd.DataFrame(out_rows), handler, map_row, stage, ctx)
    out = _finish_empty_result(mapped, src, stage)
    if handler.drops_rows:
        # The driver, not the stage, knows which input ordinals survived.
        attach_row_lineage(out, kept_rows_lineage(stage.inputs[0].id, kept_indices))
    return out


def _narrow_row(row: Row, keep: frozenset[str]) -> Row:
    return {key: value for key, value in row.items() if key in keep}


# ── the row-level cache interceptor ──────────────────────────────────────────
# Caching is a property of the handler SHAPE, not of any stage type: the one
# line where per-row compute happens is wrapped, so every row-mapped stage type
# is cached by the same code and no stage implements a cache interface. The
# cache store and its keying live below the seam (app.core.stage_cache); what
# lives here is one execution's state over it and the two decisions the runtime
# owns: WHETHER caching applies at all, and whether a given result is one the
# stage actually produced and may therefore be recorded.


class _RowCaching(NamedTuple):
    project: str
    stage_id: str
    stage_fingerprint: str
    recorded_outputs: dict[str, Row]
    writer: StageCache | None


def _open_row_caching(stage: Stage, ctx: RunContext) -> _RowCaching | None:
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
        {} if ctx.params.bust_cache else ctx.stage_cache.find_recorded_rows(
            project, stage.id, stage_fingerprint
        ),
        ctx.stage_cache if isinstance(ctx.stage_cache, StageCache) else None,
    )


def _find_cached_row(caching: _RowCaching, input_row: Row) -> Row | None:
    recorded = caching.recorded_outputs.get(compute_row_fingerprint(input_row))
    return None if recorded is None else {**recorded, ROW_CACHED_KEY: True}


def _record_row_output(caching: _RowCaching, input_row: Row, output_row: Row) -> None:
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
    return _finish_empty_result(
        _finish_batched_frame(rows, handler, stage), src, stage
    )


def _find_cached_rows_by_position(
    caching: _RowCaching, records: list[Row]
) -> dict[int, Row]:
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
    if len(computed) != len(misses):
        raise RuntimeError(
            f"stage {stage.id}: batched execution returned {len(computed)} rows "
            f"for the {len(misses)} rows it was asked to compute"
        )
    by_position = dict(hits)
    by_position.update(zip(misses, computed))
    return [by_position[position] for position in range(row_count)]


def _finish_batched_frame(
    rows: list[Row], handler: RowMapTransformHandler, stage: Stage
) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    return _strip_and_trim(df, _collect_internal_columns(df), handler, stage)


def _finish_empty_result(
    mapped: pd.DataFrame, src: pd.DataFrame, stage: Stage
) -> pd.DataFrame:
    if len(mapped.columns) > 0 or len(mapped) > 0:
        return mapped
    empty = src.iloc[0:0].copy()
    promised = stage.resolve_output_schema()
    if promised is not None:
        empty = empty.reindex(columns=[column.name for column in promised.columns])
    empty.attrs = dict(mapped.attrs)  # the StageContribution rides here
    return empty


def _finish_mapped_frame(
    df: pd.DataFrame,
    handler: RowMapTransformHandler,
    map_row: RowMapper,
    stage: Stage,
    ctx: RunContext,
) -> pd.DataFrame:
    contribution = _collect_internal_columns(df)
    if isinstance(map_row, PostMapRowMapper):
        map_row.finish_mapped_rows(stage, df, ctx, contribution)
    return _strip_and_trim(df, contribution, handler, stage)


def _collect_internal_columns(df: pd.DataFrame) -> StageContribution:
    contribution = StageContribution()
    _collect_row_errors(df, contribution)
    _collect_row_usage(df, contribution)
    _collect_cached_rows(df, contribution)
    return contribution


def _strip_and_trim(
    df: pd.DataFrame,
    contribution: StageContribution,
    handler: RowMapTransformHandler,
    stage: Stage,
) -> pd.DataFrame:
    """Strip before trim: the trim reports what it drops as user columns, which an internal one is not."""
    df = _strip_internal_columns(df)
    if handler.trims_output_to_declared:
        df = _trim_to_declared_columns(df, stage, contribution)
    df.attrs[CONTRIBUTION_ATTR] = contribution
    return df


def _strip_internal_columns(df: pd.DataFrame) -> pd.DataFrame:
    stripped = {
        internal.column
        for internal in _INTERNAL_ROW_COLUMNS
        if internal.stripped_from_output
    }
    present = [column for column in df.columns if column in stripped]
    return df.drop(columns=present) if present else df


def _consume_cancel(ctx: RunContext) -> bool:
    """Read-once: a True consumes the pending cancel, so a second call reports False."""
    if ctx.identity is None:
        return False
    return consume_cancel(ctx.identity.project, ctx.identity.run_id)


def _collect_row_errors(df: pd.DataFrame, contribution: StageContribution) -> None:
    """`pd.isna`, not truthiness: the empty string a message-less exception yields is a failure too."""
    if ROW_ERROR_KEY not in df.columns:
        return
    contribution.row_errors = [
        RowError(row=position, message=str(value))
        for position, value in enumerate(df[ROW_ERROR_KEY])
        if not pd.isna(value)
    ]


def _collect_row_usage(df: pd.DataFrame, contribution: StageContribution) -> None:
    if ROW_USAGE_KEY not in df.columns:
        return
    parts = [value for value in df[ROW_USAGE_KEY] if isinstance(value, LlmUsage)]
    contribution.llm_usage = LlmUsage.summed(parts)


def _collect_cached_rows(df: pd.DataFrame, contribution: StageContribution) -> None:
    if ROW_CACHED_KEY not in df.columns:
        return
    contribution.cached_rows = int(sum(value is True for value in df[ROW_CACHED_KEY]))


def _trim_to_declared_columns(
    df: pd.DataFrame, stage: Stage, contribution: StageContribution
) -> pd.DataFrame:
    output_schema = stage.resolve_output_schema()
    declared = [c.name for c in output_schema.columns] if output_schema else []
    if not declared:
        return df
    if not len(df.columns) and not len(df):
        # Not a violation: no mapper result named a single column, so no row
        # failed to produce a declared one. The driver gives this frame the
        # promised columns (_finish_empty_result).
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
