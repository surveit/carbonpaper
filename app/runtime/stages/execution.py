"""Handler shapes: what the runtime hands each stage type, and the row driver.

A stage type's grain-and-order guarantee follows from HOW the runtime invokes its
handler, not from the handler's body: RowMap and Source preserve (RowMap unless
registered `drops_rows`, which keeps order but not grain), Frame does not."""
from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Sequence
from typing import Any, Callable, NamedTuple, Protocol, TypeVar, runtime_checkable

import pandas as pd
import pyarrow as pa

from app.models import WorkflowStage
from app.models.run_manifest import RowError, StageContribution
from app.models.stage import (
    AbstractStage,
    StageType,
    is_grain_and_order_preserving,
    max_declared_inputs,
)
from app.models.stages.llm_transform import LLMTransformStage

from app.core.agent.usage import LlmUsage
from app.core.frames import collapse_null_forms, is_null_form, list_table_rows
from app.core.stage_cache import StageCache, compute_row_fingerprint

from .frame_caching import (
    find_cached_frame,
    note_skipped_caching,
    open_frame_caching,
    record_frame_output,
)
from ..cancellation import consume_cancel
from ..context import RunContext
from ..stage_output import StageOutput
from ..lineage import kept_rows_lineage
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


def narrow_stage(workflow_stage: WorkflowStage, model: type[_StageT]) -> _StageT:
    stage = workflow_stage.stage
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
MakeRowMapper = Callable[[WorkflowStage, RunContext, pa.Table], RowMapper]

# An LLMTransformHandler's batched execution function: the stage's inputs, the
# run, the driver's parallelism, and the INPUT POSITION of each row it is handed
# (in the order handed), which is what lets it attribute a chunk's log detail to
# the rows it actually covers. It computes one raw row per input row it is
# given — internal columns still attached, nothing stripped or trimmed — and knows
# nothing about caching: which rows it is asked about is the shape's decision
# (see `_run_batched`).
RunBatches = Callable[
    [WorkflowStage, dict[str, pa.Table], RunContext, int, list[int]], list["Row"]
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
        workflow_stage: WorkflowStage,
        rows: Sequence[Row],
        ctx: RunContext,
        contribution: StageContribution,
    ) -> None: ...


class StageHandler(ABC):
    @abstractmethod
    def execute(
        self, workflow_stage: WorkflowStage, inputs: dict[str, pa.Table],
        ctx: RunContext,
    ) -> StageOutput | None: ...

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
    ) -> None:
        self.make_mapper = make_mapper
        self.parallelism = parallelism
        self.trims_output_to_declared = trims_output_to_declared
        self.drops_rows = drops_rows

    def execute(
        self, workflow_stage: WorkflowStage, inputs: dict[str, pa.Table],
        ctx: RunContext,
    ) -> StageOutput:
        return _run_row_mapper(self, workflow_stage, inputs, ctx)

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
        self, workflow_stage: WorkflowStage, inputs: dict[str, pa.Table],
        ctx: RunContext,
    ) -> StageOutput:
        if narrow_stage(workflow_stage, LLMTransformStage).llm.batch_size > 1:
            return _run_batched(self, workflow_stage, inputs, ctx)
        return _run_row_mapper(self, workflow_stage, inputs, ctx)


class SourceHandler(StageHandler):
    def __init__(
        self, read: Callable[[WorkflowStage, RunContext], pd.DataFrame]
    ) -> None:
        self.read = read

    def execute(
        self, workflow_stage: WorkflowStage, inputs: dict[str, pa.Table],
        ctx: RunContext,
    ) -> StageOutput:
        return StageOutput.from_frame(self.read(workflow_stage, ctx))

    @property
    def preserves_grain_and_order(self) -> bool:
        return True


class FrameTransformHandler(StageHandler):
    def __init__(
        self,
        apply: Callable[
            [WorkflowStage, dict[str, pa.Table], RunContext], StageOutput | None
        ],
        caches_frames: bool = True,
    ) -> None:
        self.apply = apply
        self.caches_frames = caches_frames

    def execute(
        self, workflow_stage: WorkflowStage, inputs: dict[str, pa.Table],
        ctx: RunContext,
    ) -> StageOutput | None:
        caching = open_frame_caching(workflow_stage, ctx, self.caches_frames)
        if caching.key is None:
            output = self.apply(workflow_stage, inputs, ctx)
            return note_skipped_caching(output, caching.skipped_note)
        input_tables = [inputs[ref.id] for ref in workflow_stage.inputs]
        cached = find_cached_frame(caching, input_tables)
        if cached is not None:
            # A replayed frame carries no contribution: nothing ran to report.
            return StageOutput(cached)
        return record_frame_output(
            caching, input_tables, self.apply(workflow_stage, inputs, ctx)
        )

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
    workflow_stage: WorkflowStage,
    inputs: dict[str, pa.Table],
    ctx: RunContext,
) -> StageOutput:
    """One result slot per input row, filled by index: grain and order hold by construction."""
    stage = workflow_stage.stage
    src = inputs[workflow_stage.inputs[0].id]
    reads = stage.anchor_reads()
    map_row = handler.make_mapper(workflow_stage, ctx, src)
    # The ONE line of per-row compute, optionally routed through the row cache
    # and the run log. Log outside cache, so a row the cache answers never
    # reaches the mapper's lifecycle wrapper and is logged as the replay it is.
    # `map_row` itself stays bound: _finish_mapped_frame tests it for the
    # PostMapRowMapper shape, which a wrapper would hide.
    caching = _open_row_caching(workflow_stage, ctx)
    compute_row = _log_row_lifecycle(map_row, ctx.run_log, stage.id)
    if caching is not None:
        compute_row = _map_row_through_cache(caching, compute_row, ctx.run_log, stage.id)
    records = list_table_rows(src)
    seen = [_narrow_row(row, reads) for row in records]
    progress = ctx.stage_progress
    completed = 0
    progress(completed=completed, total=len(records))

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
                completed += 1
                progress(completed=completed, total=len(records))
    else:
        for index, record in enumerate(seen):
            if _consume_cancel(ctx):
                raise RunCancelled(f"stage {stage.id}: cancelled")
            results[index] = compute_row(record, index)
            completed += 1
            progress(completed=completed, total=len(records))

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
    mapped = _finish_mapped_frame(out_rows, handler, map_row, workflow_stage, ctx)
    # The driver, not the stage, knows which input ordinals survived.
    lineage = (
        kept_rows_lineage(workflow_stage.inputs[0].id, kept_indices)
        if handler.drops_rows
        else None
    )
    return StageOutput(
        _finish_empty_result(mapped.table, src, workflow_stage),
        mapped.contribution,
        lineage,
    )


def _narrow_row(row: Row, keep: frozenset[str]) -> Row:
    return {key: value for key, value in row.items() if key in keep}


def _narrow_table(table: pa.Table, keep: frozenset[str]) -> pa.Table:
    return table.select([name for name in table.column_names if name in keep])


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


def _open_row_caching(workflow_stage: WorkflowStage, ctx: RunContext) -> _RowCaching | None:
    stage = workflow_stage.stage
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
        project_id=caching.project,
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
    workflow_stage: WorkflowStage,
    inputs: dict[str, pa.Table],
    ctx: RunContext,
) -> StageOutput:
    """Runs only the rows the cache cannot answer, then reorders them into INPUT order."""
    stage = workflow_stage.stage
    src = inputs[workflow_stage.inputs[0].id]
    records = list_table_rows(src)
    # Narrowed exactly as the row-mapped driver narrows, so a chunk of N rows is
    # N rows of the shape the row cache already keys and stores. Without this the
    # key covers every input column, so an edit to a column the signature does
    # not read — one the prompt therefore cannot reference — re-spends the stage.
    narrowed = _narrow_table(src, stage.anchor_reads())
    seen = list_table_rows(narrowed)
    progress = ctx.stage_progress
    caching = _open_row_caching(workflow_stage, ctx)
    hits = {} if caching is None else _find_cached_rows_by_position(caching, seen)
    progress(completed=len(hits), total=len(records))
    misses = [index for index in range(len(records)) if index not in hits]
    emit_batched_row_starts(ctx.run_log, stage.id, hits, misses)
    computed = _compute_batched_rows(handler, workflow_stage, narrowed, misses, ctx)
    # Ordered before recorded: the ordering step is what verifies one computed
    # row per miss, so nothing is pinned against a row it did not come from.
    mapped = _order_by_input_position(stage.id, hits, misses, computed, len(records))
    if caching is not None:
        for position, row in zip(misses, computed):
            _record_row_output(caching, seen[position], row)
    emit_batched_row_outcomes(
        ctx.run_log, stage.id, misses, [row.get(ROW_ERROR_KEY) for row in computed]
    )
    # Rejoin, as the row-mapped driver does: the model only ever saw the declared
    # reads, so the columns that merely FLOW come back from the input row here.
    rows = [{**records[index], **row} for index, row in enumerate(mapped)]
    batched = _finish_batched_frame(rows, handler, workflow_stage)
    return StageOutput(
        _finish_empty_result(batched.table, src, workflow_stage), batched.contribution
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
    workflow_stage: WorkflowStage,
    src: pa.Table,
    misses: list[int],
    ctx: RunContext,
) -> list[Row]:
    if not misses:
        return []
    return handler.run_batches(
        workflow_stage,
        {workflow_stage.inputs[0].id: src.take(misses)},
        ctx,
        handler.parallelism,
        misses,
    )


def _order_by_input_position(
    stage_id: str,
    hits: dict[int, Row],
    misses: list[int],
    computed: list[Row],
    row_count: int,
) -> list[Row]:
    if len(computed) != len(misses):
        raise RuntimeError(
            f"stage {stage_id}: batched execution returned {len(computed)} rows "
            f"for the {len(misses)} rows it was asked to compute"
        )
    by_position = dict(hits)
    by_position.update(zip(misses, computed))
    return [by_position[position] for position in range(row_count)]


class _MappedRows(NamedTuple):
    """The row driver's intermediate: the assembled table and what its rows reported."""

    table: pa.Table
    contribution: StageContribution


def _finish_batched_frame(
    rows: list[Row], handler: RowMapTransformHandler, workflow_stage: WorkflowStage
) -> _MappedRows:
    """No mapper, so no post-map step: collect off the rows, then strip and trim."""
    contribution = _collect_internal_columns(rows)
    return _MappedRows(
        _strip_and_trim(rows, contribution, handler, workflow_stage), contribution
    )


def _finish_empty_result(
    mapped: pa.Table, src: pa.Table, workflow_stage: WorkflowStage
) -> pa.Table:
    if mapped.num_columns > 0 or mapped.num_rows > 0:
        return mapped
    promised = workflow_stage.output_schema
    if promised is None:
        return src.schema.empty_table()
    # A promised column the input carried keeps the input's type; one the stage
    # would have added has no values to type, so it is null.
    fields = [
        src.schema.field(column.name)
        if column.name in src.schema.names
        else pa.field(column.name, pa.null())
        for column in promised.columns
    ]
    return pa.schema(fields).empty_table()


def _finish_mapped_frame(
    rows: Sequence[Row],
    handler: RowMapTransformHandler,
    map_row: RowMapper,
    workflow_stage: WorkflowStage,
    ctx: RunContext,
) -> _MappedRows:
    """`finish_mapped_rows` is the last step that sees an internal column: it runs before the strip."""
    contribution = _collect_internal_columns(rows)
    if isinstance(map_row, PostMapRowMapper):
        map_row.finish_mapped_rows(workflow_stage, rows, ctx, contribution)
    return _MappedRows(
        _strip_and_trim(rows, contribution, handler, workflow_stage), contribution
    )


def _collect_internal_columns(rows: Sequence[Row]) -> StageContribution:
    contribution = StageContribution()
    _collect_row_errors(rows, contribution)
    _collect_row_usage(rows, contribution)
    _collect_cached_rows(rows, contribution)
    return contribution


def _strip_and_trim(
    rows: Sequence[Row],
    contribution: StageContribution,
    handler: RowMapTransformHandler,
    workflow_stage: WorkflowStage,
) -> pa.Table:
    """Strip before trim: the trim reports what it drops as user columns, which an internal one is not."""
    table = pa.Table.from_pylist(_strip_internal_columns(rows))
    if handler.trims_output_to_declared:
        table = _trim_to_declared_columns(workflow_stage, table, contribution)
    return table


def _strip_internal_columns(rows: Sequence[Row]) -> list[Row]:
    stripped = {
        internal.column
        for internal in _INTERNAL_ROW_COLUMNS
        if internal.stripped_from_output
    }
    # `collapse_null_forms` too: a mapper may hand back pd.NA or NaT, which arrow
    # cannot type, and which this codebase reads as absent anyway.
    return [
        {k: collapse_null_forms(v) for k, v in row.items() if k not in stripped}
        for row in rows
    ]


def _consume_cancel(ctx: RunContext) -> bool:
    """Read-once: a True consumes the pending cancel, so a second call reports False."""
    if ctx.identity is None:
        return False
    return consume_cancel(ctx.identity.project, ctx.identity.run_id)


def _collect_row_errors(rows: Sequence[Row], contribution: StageContribution) -> None:
    """`is_null_form`, not truthiness: the empty string a message-less exception yields is a failure too."""
    if not any(ROW_ERROR_KEY in row for row in rows):
        return
    contribution.row_errors = [
        RowError(row=position, message=str(row[ROW_ERROR_KEY]))
        for position, row in enumerate(rows)
        if ROW_ERROR_KEY in row and not is_null_form(row[ROW_ERROR_KEY])
    ]


def _collect_row_usage(rows: Sequence[Row], contribution: StageContribution) -> None:
    if not any(ROW_USAGE_KEY in row for row in rows):
        return
    parts = [row[ROW_USAGE_KEY] for row in rows if isinstance(row.get(ROW_USAGE_KEY), LlmUsage)]
    contribution.llm_usage = LlmUsage.summed(parts)


def _collect_cached_rows(rows: Sequence[Row], contribution: StageContribution) -> None:
    # Only when a row carried the marker: no hits reports no count, not a zero.
    if not any(ROW_CACHED_KEY in row for row in rows):
        return
    contribution.cached_rows = sum(1 for row in rows if row.get(ROW_CACHED_KEY) is True)


def _trim_to_declared_columns(
    workflow_stage: WorkflowStage, table: pa.Table, contribution: StageContribution
) -> pa.Table:
    output_schema = workflow_stage.output_schema
    declared = [c.name for c in output_schema.columns] if output_schema else []
    if not declared:
        return table
    if not table.num_columns and not table.num_rows:
        # Not a violation: no mapper result named a single column, so no row
        # failed to produce a declared one. The driver gives this table the
        # promised columns (_finish_empty_result).
        return table
    missing = [name for name in declared if name not in table.column_names]
    if missing and not contribution.row_errors:
        raise ValueError(
            f"stage '{workflow_stage.id}' declares output column(s) {missing} that it did not "
            f"produce; the frame carries {list(table.column_names)}. A declared "
            "column is what downstream stages are entitled to read — it can be neither "
            "invented nor dropped from the projection."
        )
    dropped = [name for name in table.column_names if name not in declared]
    if dropped:
        contribution.dropped_columns = dropped
    for name in missing:
        table = table.append_column(name, pa.nulls(table.num_rows))
    return table.select(declared)
