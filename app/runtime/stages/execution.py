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
from pydantic import BaseModel

from app.models import WorkflowStage
from app.models.run_manifest import RowError, StageContribution
from app.models.stages.signature import transform_output_schema
from app.models.stage import (
    AbstractStage,
    StageType,
    find_cache_ignored_reason,
    is_grain_and_order_preserving,
    max_declared_inputs,
)

from app.core.agent.usage import LlmUsage
from app.core.frames import collapse_null_forms, is_null_form, list_table_rows
from app.core.stage_cache import StageCache, compute_row_fingerprint
from ..branches import BranchRecorder

from .frame_caching import (
    find_cached_frame,
    note_skipped_caching,
    open_frame_caching,
    record_frame_output,
)
from ..cancellation import consume_cancel
from ..lease import validate_still_held
from ..context import RunContext
from ..stage_output import StageOutput
from ..lineage import kept_rows_lineage
from ..errors import RunCancelled
from ..run_log import RunLog, bind_detail_sink, unbind_detail_sink
from ..validation import build_row_model, find_row_issues
from .row_events import (
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

# What the driver actually dispatches: a GROUP of rows and their input positions
# in, one raw row per row in — internal columns still attached, nothing stripped
# or trimmed, None to drop. Every row-mapped type is driven through this; the
# group is one row wide unless the stage asks for more (see `group_size`), which
# only a batched `llm_transform` does. A mapper knows nothing about caching:
# which rows it is asked about is the driver's decision.
GroupMapper = Callable[[Sequence[int], Sequence["Row"]], "Sequence[Row | None]"]

# Internal column a row mapper attaches to a row it could not produce (e.g. an
# llm_transform whose generation failed). The row driver collects these off the
# assembled frame so the runner can surface them as error-severity output issues
# — a failed row is a reported error, not a silently dropped column.
ROW_ERROR_KEY = "_error"

# Internal column carrying a row's token/cost usage dict (an llm_transform
# attaches one per row). It is summed onto the stage's StageContribution and the
# column is then stripped, so usage never reaches stage output.
ROW_USAGE_KEY = "_usage"

# Internal column a row mapper attaches to a row whose value could not be
# produced synchronously: the value does not exist yet, so the run cannot be
# carried past this stage. Distinct from ROW_ERROR_KEY, which marks a row that
# FAILED and lets the run continue. The driver never interprets it — a mapper
# that emits it reads it back in its own `finish_mapped_rows`.
ROW_DEFERRED_KEY = "_deferred"

# Internal column marking a row the row cache answered rather than the stage
# computing it. Stamped where the hit is read; the driver counts them onto the
# stage's StageContribution. A computed row never
# carries the column, so a stage with no hits reports no count rather than a zero.
ROW_CACHED_KEY = "_cached"


class _InternalRowColumn(NamedTuple):
    column: str
    # Machinery, not stage output: dropped off every mapped frame, and NOT
    # reported as a dropped user column (it was collected by the driver or read
    # back by the mapper's own post-map step, not discarded).
    stripped_from_output: bool
    # Marks a row that is not an output the stage produced, so it must never be
    # pinned as its input key's answer.
    blocks_caching: bool


# The ONE declaration of the internal row columns: `_strip_internal_columns` and
# `_cache_row_output` read the two behaviors off this table.
_INTERNAL_ROW_COLUMNS = (
    _InternalRowColumn(ROW_ERROR_KEY, stripped_from_output=True, blocks_caching=True),
    _InternalRowColumn(ROW_USAGE_KEY, stripped_from_output=True, blocks_caching=False),
    _InternalRowColumn(ROW_DEFERRED_KEY, stripped_from_output=True, blocks_caching=True),
    _InternalRowColumn(ROW_CACHED_KEY, stripped_from_output=True, blocks_caching=False),
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

    def group_size(self, workflow_stage: WorkflowStage) -> int:
        """Rows per mapper call."""
        return 1

    def make_group_mapper(
        self, workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
    ) -> GroupMapper:
        return _RowsInGroupsOfOne(self.make_mapper(workflow_stage, ctx, src))

    @property
    def preserves_grain_and_order(self) -> bool:
        return not self.drops_rows


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
        reads_cache = _reads_the_cache_flag(handler)
        declared_reason = find_cache_ignored_reason(stage_type)
        if reads_cache != (declared_reason is None):
            raise RuntimeError(
                f"stage type {stage_type.value!r} is registered as "
                f"{type(handler).__name__} (reads `cache`={reads_cache}), but its model "
                f"declares CACHE_IGNORED_BECAUSE={declared_reason!r}"
            )


def _reads_the_cache_flag(handler: StageHandler) -> bool:
    """A source recomputes; a frame handler may refuse; a row mapper always consults."""
    if isinstance(handler, SourceHandler):
        return False
    if isinstance(handler, FrameTransformHandler):
        return handler.caches_frames
    return True


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
    # `map_group` stays bound here: _finish_mapped_frame tests it for the
    # PostMapRowMapper shape, which _StageExecution would hide.
    map_group = handler.make_group_mapper(workflow_stage, ctx, src)
    caching = _open_row_caching(workflow_stage, ctx)
    execution = _StageExecution(
        map_group,
        build_row_model(transform_output_schema(stage), f"{stage.id}_written"),
        caching,
        ctx.run_log,
        stage.id,
    )
    input_rows = list_table_rows(src)
    narrowed_rows = [_narrow_row(row, reads) for row in input_rows]
    # Answered before the grouping, so a hit never takes a seat in a model call
    # and never reaches the mapper — it is logged as the replay it is.
    cached_results: list[Row | None] = (
        [None] * len(narrowed_rows)
        if caching is None
        else _find_cached_rows(caching, narrowed_rows, ctx.run_log, stage.id)
    )
    results = _fan_out(
        handler, execution, narrowed_rows, cached_results, workflow_stage, ctx
    )

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
        out_rows.append({**input_rows[index], **result})
        kept_indices.append(index)
    mapped = _finish_mapped_frame(out_rows, handler, map_group, workflow_stage, ctx)
    recorder = map_group.branch_recorder if isinstance(map_group, _RowsInGroupsOfOne) else None
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
        recorder.rows_kept(kept_indices) if recorder else None,
    )


class RecordingRowMapper:
    """A row mapper plus the recorder its stage's code reports into."""

    def __init__(self, map_row: RowMapper, recorder: BranchRecorder) -> None:
        self.map_row = map_row
        self.branch_recorder = recorder

    def __call__(self, row: Row, index: int) -> Row | None:
        return self.map_row(row, index)


class _RowsInGroupsOfOne:
    """A per-row mapper as a group mapper. An object, not a closure, so a post-map step survives."""

    def __init__(self, map_row: RowMapper) -> None:
        self.map_row = map_row
        self.branch_recorder = (
            map_row.branch_recorder if isinstance(map_row, RecordingRowMapper) else None
        )

    def __call__(
        self, indices: Sequence[int], rows: Sequence[Row]
    ) -> Sequence[Row | None]:
        mapped: list[Row | None] = []
        for index, row in zip(indices, rows):
            if self.branch_recorder is not None:
                self.branch_recorder.open_row(index)
            try:
                mapped.append(self.map_row(row, index))
            finally:
                # Left open, a raising row would lend its branches to the next.
                if self.branch_recorder is not None:
                    self.branch_recorder.close_row()
        return mapped

    def finish_mapped_rows(
        self,
        workflow_stage: WorkflowStage,
        rows: Sequence[Row],
        ctx: RunContext,
        contribution: StageContribution,
    ) -> None:
        if isinstance(self.map_row, PostMapRowMapper):
            self.map_row.finish_mapped_rows(workflow_stage, rows, ctx, contribution)


# ── the fan-out ──────────────────────────────────────────────────────────────
# The ONE thing that differs between a row-at-a-time stage and a batched one:
# how many rows reach the mapper per call. Everything on either side — the cache
# lookup, the recording, the row lifecycle, the slot fill, the rejoin — is the
# same code for both, which is why a batched stage banks each group as it
# completes without any batch-specific persistence logic.


def _fan_out(
    handler: RowMapTransformHandler,
    execution: "_StageExecution",
    narrowed_rows: list[Row],
    cached_results: list[Row | None],
    workflow_stage: WorkflowStage,
    ctx: RunContext,
) -> list[Row | None]:
    """Computes the rows the cache left empty; returns one result per input row."""
    stage_id = workflow_stage.stage.id
    results = list(cached_results)
    total = len(results)
    pending = [index for index, cached in enumerate(results) if cached is None]
    groups = _split_into_groups(
        pending, narrowed_rows, handler.group_size(workflow_stage)
    )
    progress = ctx.stage_progress
    # Every row the cache answered is already done, and is never dispatched.
    completed = total - len(pending)
    progress(completed=completed, total=total)
    if handler.parallelism > 1 and len(groups) > 1:
        with ThreadPoolExecutor(max_workers=handler.parallelism) as pool:
            futures = {
                pool.submit(execution.run_group, indices, rows): indices
                for indices, rows in groups
            }
            try:
                for future in as_completed(futures):
                    # Cancel first: every instruction before it is a wider window for the
                    # pool to dispatch another chunk, and that costs a model call.
                    if _consume_cancel(ctx):
                        raise RunCancelled(f"stage {stage_id}: cancelled mid-fan-out")
                    validate_still_held()
                    indices = futures[future]
                    _place_group(results, indices, future.result(), stage_id)
                    completed += len(indices)
                    progress(completed=completed, total=total)
            finally:
                # Drop every group not yet started. On EVERY exit, not just the
                # cancel: every group is submitted up front, so `max_workers`
                # bounds concurrency but not the queue, and the `with` block's
                # own shutdown(wait=True) would otherwise run the whole backlog
                # before the exception surfaced — a model call per queued group,
                # paid for and then dropped, since nobody is left reading the
                # results. Groups already dispatched (<= parallelism) keep
                # running in their worker threads, since a blocking call can't
                # be killed, and are joined on the way out.
                pool.shutdown(wait=False, cancel_futures=True)
    else:
        for indices, rows in groups:
            if _consume_cancel(ctx):
                raise RunCancelled(f"stage {stage_id}: cancelled")
            validate_still_held()
            _place_group(
                results, indices, execution.run_group(indices, rows), stage_id
            )
            completed += len(indices)
            progress(completed=completed, total=total)
    return results


def _split_into_groups(
    pending: list[int], narrowed_rows: list[Row], size: int
) -> list[tuple[tuple[int, ...], list[Row]]]:
    """Groups the PENDING positions, so a cache hit never takes a seat in a model call."""
    return [
        (tuple(pending[start : start + size]),
         [narrowed_rows[index] for index in pending[start : start + size]])
        for start in range(0, len(pending), size)
    ]


def _place_group(
    results: list[Row | None],
    indices: Sequence[int],
    group: Sequence[Row | None],
    stage_id: str,
) -> None:
    for index, row in zip(indices, _require_one_row_each(group, indices, stage_id)):
        results[index] = row


def _require_one_row_each(
    group: Sequence[Row | None], indices: Sequence[int], stage_id: str
) -> Sequence[Row | None]:
    """Nothing is pinned or placed against a row it did not come from."""
    if len(group) != len(indices):
        raise RuntimeError(
            f"stage {stage_id}: mapper returned {len(group)} row(s) for "
            f"{len(indices)} input row(s)"
        )
    return group


def _narrow_row(row: Row, keep: frozenset[str]) -> Row:
    return {key: value for key, value in row.items() if key in keep}


def _narrow_table(table: pa.Table, keep: frozenset[str]) -> pa.Table:
    return table.select([name for name in table.column_names if name in keep])


# ── the row-level cache interceptor ──────────────────────────────────────────
# Caching is a property of the handler SHAPE, not of any stage type: the driver
# reads before it groups and wraps the mapper to record, so every row-mapped
# stage type is cached by the same code and no stage implements one. The
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


def _cache_row_output(caching: _RowCaching, input_row: Row, output_row: Row) -> None:
    if caching.writer is None:
        return
    if _blocks_caching(output_row):
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


def _find_cached_rows(
    caching: _RowCaching, narrowed_rows: list[Row], log: RunLog | None, stage_id: str
) -> list[Row | None]:
    """Answered BEFORE grouping: a hit taking a seat in a model call is a batch paid for and wasted."""
    cached_results: list[Row | None] = []
    for index, row in enumerate(narrowed_rows):
        hit = _find_cached_row(caching, row)
        if hit is not None:
            emit_cached_row(log, stage_id, index)
        cached_results.append(hit)
    return cached_results


# ── one group of rows, start to finish ───────────────────────────────────────


class _StageExecution(NamedTuple):
    """What one execution needs to turn a group of input rows into results."""

    map_group: GroupMapper
    written_model: type[BaseModel]
    caching: _RowCaching | None
    log: RunLog | None
    stage_id: str

    def run_group(
        self, indices: Sequence[int], rows: Sequence[Row]
    ) -> Sequence[Row | None]:
        """Map, validate, log, record. One function because the ORDER is the content."""
        for index in indices:
            emit_row_start(self.log, self.stage_id, index)
        # Bound here, on the worker thread that makes the call: a pool thread
        # starts with an empty context, so a bind made outside would be lost. It
        # lets `llm.py` attribute its prompt/response to these rows several frames
        # down without a log being threaded through every mapper.
        token = bind_detail_sink(self.log, self.stage_id, tuple(indices))
        try:
            mapped = self.map_group(indices, rows)
        except Exception as exc:  # noqa: BLE001 — logged, then re-raised unchanged;
            # the executor's own per-stage handling is untouched.
            for index in indices:
                emit_row_raised(self.log, self.stage_id, index, exc)
            raise
        finally:
            unbind_detail_sink(token)

        results = [
            _validate_row(_assert_row(row, self.stage_id), self.written_model)
            for row in mapped
        ]
        for index, result in zip(indices, results):
            # A dropped row (None) ran to completion — it has no error to report.
            emit_row_outcome(
                self.log,
                self.stage_id,
                index,
                result.get(ROW_ERROR_KEY) if result else None,
            )
        if self.caching is not None:
            for row, result in zip(rows, results):
                # A drop is not a recordable output: the store holds output ROWS,
                # so a replayed drop would be indistinguishable from a miss. A row
                # this function just failed carries _error, which also blocks it.
                if result is not None:
                    _cache_row_output(self.caching, row, result)
        return results


def _assert_row(row: object, stage_id: str) -> Row | None:
    """Caught the moment the mapper returns, so nothing downstream defends against a non-row."""
    if row is None or isinstance(row, dict):
        return row
    raise ValueError(
        f"stage {stage_id}: row mapper must return one dict per row, "
        f"got {type(row).__name__}"
    )


def _validate_row(row: Row | None, model: type[BaseModel]) -> Row | None:
    if row is None or _blocks_caching(row):
        return row
    issues = find_row_issues(row, model)
    if not issues:
        return row
    return {**row, ROW_ERROR_KEY: "; ".join(issues)}


def _blocks_caching(row: Row) -> bool:
    return any(
        row.get(internal.column) is not None
        for internal in _INTERNAL_ROW_COLUMNS
        if internal.blocks_caching
    )


class _MappedRows(NamedTuple):
    """The row driver's intermediate: the assembled table and what its rows reported."""

    table: pa.Table
    contribution: StageContribution


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
    map_group: GroupMapper,
    workflow_stage: WorkflowStage,
    ctx: RunContext,
) -> _MappedRows:
    """`finish_mapped_rows` is the last step that sees an internal column: it runs before the strip."""
    contribution = _collect_internal_columns(rows)
    if isinstance(map_group, PostMapRowMapper):
        map_group.finish_mapped_rows(workflow_stage, rows, ctx, contribution)
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
