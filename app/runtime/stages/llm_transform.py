"""Execution for the llm_transform stage type, split by `batch_size`. Both paths
hold grain and order; only the per-row path (== 1) also holds per-row
INDEPENDENCE - a batched call (> 1) shows the model every row in the chunk.
Replies rejoin by a batch-local 0-based row number the runtime assigns, never
the input primary key (which the runtime does not require to exist or be unique).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import pyarrow as pa
from pydantic import create_model

from app.core.agent.usage import LlmUsage
from app.core.frames import list_table_rows
from app.core.llm import LLMModel
from app.models import WorkflowStage
from app.models.schema import Column, TableSchema
from app.models.stages.llm_transform import LLMTransformStage

from ..context import RunContext
from ..llm import call_llm, call_llm_batch, render_prompt
from ..run_log import RunLog, bind_detail_sink, unbind_detail_sink

from .execution import ROW_ERROR_KEY, ROW_USAGE_KEY, Row, RowMapper, narrow_stage

# The reply field carrying a batched result's item number — the rejoin handle.
# Runtime-assigned per chunk (0-based), so it is always a small unique int the
# model just has to copy; it never touches the input primary key.
_ROW_NUMBER_FIELD = "row_number"


# ── batch_size == 1: per-row path (grain + order + independence by construction) ──
def make_llm_row_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
) -> RowMapper:
    """A row's reply depends only on that row: neither the frame nor the row's position is read."""
    stage = narrow_stage(workflow_stage, LLMTransformStage)
    llm = stage.llm
    selected_model = ctx.require_selected_llm_transform_model()

    # What the model is asked for: the columns the signature adds, compiled to the
    # model the agent must satisfy. Its input columns are rejoined by the driver.
    reply_spec = TableSchema(columns=stage.signature.adds)
    reply_model = reply_spec.to_pydantic_model(f"{stage.id}_reply")

    def map_row(row: Row, index: int) -> Row:
        usages: list[LlmUsage] = []
        try:
            reply = call_llm(
                stage.id,
                llm,
                row,
                reply_model=reply_model,
                model=selected_model,
                usage_out=usages,
            )
        except Exception as exc:  # noqa: BLE001 — per-row supervisor: tag the row
            # with the ROW_ERROR_KEY sentinel so the map completes (one bad row
            # does not abort the stage); the row driver collects these off the
            # assembled frame and the runner surfaces them as error-severity
            # output issues. Falls back to the exception's type name when its
            # message is empty (e.g. a bare TimeoutError), so a message-less
            # failure still reads as a failure rather than an empty-string cell.
            return {**row, ROW_ERROR_KEY: str(exc) or type(exc).__name__,
                    ROW_USAGE_KEY: LlmUsage.summed(usages)}
        return {**row, **reply, ROW_USAGE_KEY: LlmUsage.summed(usages)}

    return map_row


# ── batch_size > 1: batched path (grain + order preserved and VERIFIED) ──
def run_llm_batches(
    workflow_stage: WorkflowStage,
    inputs: dict[str, pa.Table],
    ctx: RunContext,
    parallelism: int,
    positions: list[int],
) -> list[Row]:
    stage = narrow_stage(workflow_stage, LLMTransformStage)
    llm = stage.llm
    selected_model = ctx.require_selected_llm_transform_model()
    batch_reply_schema = _build_batch_reply_schema(stage)

    src = inputs[workflow_stage.inputs[0].id]
    records: list[Row] = list_table_rows(src)

    results: list[Row | None] = [None] * len(records)
    process_chunk = _build_chunk_processor(
        stage, llm, selected_model, batch_reply_schema, positions, ctx.run_log
    )
    for index, row in _run_chunks(records, llm.batch_size, parallelism, process_chunk):
        results[index] = row

    # Grain + order guarantee, verified not assumed: exactly one row per input,
    # every slot filled, in input order. A gap here is a batch-driver bug, raised
    # loudly — never a result with the wrong row count.
    if len(results) != len(records) or any(row is None for row in results):
        raise RuntimeError(
            f"stage {stage.id}: batched execution did not produce exactly one row "
            f"per input row ({len(records)} in)"
        )
    return [row for row in results if row is not None]


def _build_chunk_processor(
    stage: LLMTransformStage,
    llm: Any,
    selected_model: LLMModel,
    batch_reply_schema: type,
    positions: list[int],
    log: RunLog | None,
) -> Callable[[int, list[Row]], list[tuple[int, Row]]]:
    """Bound on the worker thread: a pool thread starts with an empty context, so an outer bind is lost."""
    def process_chunk(start: int, chunk: list[Row]) -> list[tuple[int, Row]]:
        token = bind_detail_sink(
            log, stage.id, tuple(positions[start : start + len(chunk)])
        )
        try:
            return _process_chunk(
                stage.id, llm, selected_model, batch_reply_schema, start, chunk
            )
        finally:
            unbind_detail_sink(token)

    return process_chunk


def _run_chunks(
    records: list[Row],
    size: int,
    parallelism: int,
    process_chunk: Callable[[int, list[Row]], list[tuple[int, Row]]],
) -> list[tuple[int, Row]]:
    chunks = [
        (start, records[start : start + size]) for start in range(0, len(records), size)
    ]
    computed: list[tuple[int, Row]] = []
    if parallelism > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = [pool.submit(process_chunk, start, chunk) for start, chunk in chunks]
            for future in as_completed(futures):
                computed.extend(future.result())
    else:
        for start, chunk in chunks:
            computed.extend(process_chunk(start, chunk))
    return computed


def _build_batch_reply_schema(stage: LLMTransformStage) -> type:
    reply_spec = TableSchema(columns=stage.signature.adds)
    number_column = Column(
        name=_ROW_NUMBER_FIELD, type="int", nullable=False,
        description=(
            "The item number shown in the prompt (0-based). Copy it exactly onto "
            "this result so it is matched back to its item."
        ),
    )
    item_schema = TableSchema(columns=[number_column, *reply_spec.columns])
    item_reply = item_schema.to_pydantic_model(f"{stage.id}_batch_item")
    return create_model(f"{stage.id}_batch", results=(list[item_reply], ...))  # type: ignore[valid-type]


def _process_chunk(
    stage_id: str,
    llm: Any,
    selected_model: LLMModel,
    batch_reply_schema: type,
    start: int,
    chunk: list[Row],
) -> list[tuple[int, Row]]:
    """A confused reply fails EVERY row of the chunk: the answers that matched are not trusted."""
    n = len(chunk)
    usages: list[LlmUsage] = []
    problem = "no reply produced"
    attempts = max(1, (llm.max_retries or 0) + 1)
    for attempt in range(attempts):
        task = _render_batch_task(llm.prompt_data_template, chunk, correction=problem if attempt else None)
        try:
            reply = call_llm_batch(
                stage_id, llm, instructions=llm.prompt_instructions, task=task,
                reply_schema=batch_reply_schema, model=selected_model, usage_out=usages,
            )
        except Exception as exc:  # noqa: BLE001 — a chunk that never returns is thrown back, then errored
            problem = str(exc) or type(exc).__name__
            continue
        by_number, problem = _validate_batch_reply(reply.get("results", []), n)
        if by_number is not None:
            return _emit_matched(start, chunk, by_number, usages)
        # anomaly → loop and re-call the model (throw back)
    return _emit_failed(start, chunk, usages,
                        f"batched reply invalid after {attempts} attempt(s): {problem}")


def _validate_batch_reply(
    results: list[dict[str, Any]], n: int
) -> tuple[dict[int, dict[str, Any]] | None, str]:
    """A count check is not enough: a duplicate plus a miss passes on length, so check the multiset."""
    numbers = [item.get(_ROW_NUMBER_FIELD) for item in results]
    if len(numbers) == n and sorted(x for x in numbers if isinstance(x, int)) == list(range(n)):
        return {int(item[_ROW_NUMBER_FIELD]): item for item in results}, ""
    valid = set(range(n))
    present = [x for x in numbers if isinstance(x, int)]
    missing = sorted(valid - set(present))
    unknown = sorted({x for x in numbers if not isinstance(x, int) or x not in valid}, key=str)
    duplicate = sorted({x for x in present if present.count(x) > 1})
    return None, (
        f"expected one result per item 0..{n - 1}; "
        f"missing={missing}, unknown={unknown}, duplicate={duplicate}"
    )


def _emit_matched(
    start: int, chunk: list[Row], by_number: dict[int, dict[str, Any]], usages: list[LlmUsage]
) -> list[tuple[int, Row]]:
    """Usage is per-call: the whole chunk's usage lands on its first row, the rest carry zero."""
    total = LlmUsage.summed(usages)
    out: list[tuple[int, Row]] = []
    for offset, row in enumerate(chunk):
        reply_fields = {k: v for k, v in by_number[offset].items() if k != _ROW_NUMBER_FIELD}
        usage = total if offset == 0 else LlmUsage()
        out.append((start + offset, {**row, **reply_fields, ROW_USAGE_KEY: usage}))
    return out


def _emit_failed(
    start: int, chunk: list[Row], usages: list[LlmUsage], message: str
) -> list[tuple[int, Row]]:
    total = LlmUsage.summed(usages)
    return [
        (start + offset,
         {**row, ROW_ERROR_KEY: message, ROW_USAGE_KEY: total if offset == 0 else LlmUsage()})
        for offset, row in enumerate(chunk)
    ]


def _render_batch_task(template: str, chunk: list[Row], correction: str | None = None) -> str:
    items = [f"### item {offset}\n{render_prompt(template, row)}" for offset, row in enumerate(chunk)]
    instruction = (
        f"The {len(chunk)} items above are numbered 0..{len(chunk) - 1}. Return exactly "
        f"one result per item, each with its item number ({_ROW_NUMBER_FIELD}) copied "
        "exactly — one result per item number, no more, no fewer."
    )
    if correction:
        instruction += (
            f"\n\nYour previous reply was rejected: {correction}. Return exactly one "
            f"result for every item number 0..{len(chunk) - 1}."
        )
    return "\n\n".join(items) + "\n\n---\n" + instruction
