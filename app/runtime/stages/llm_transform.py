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
from app.models import Stage
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
def make_llm_row_mapper(stage: Stage, ctx: RunContext, src: pa.Table) -> RowMapper:
    """Build this execution's per-row mapper. A row's reply depends only on that
    row, so neither the input frame nor a row's position in it is read."""
    llm = narrow_stage(stage, LLMTransformStage).llm

    # What the model is asked for: the columns the signature adds, compiled to the
    # model the agent must satisfy. Its input columns are rejoined by the driver.
    reply_spec = TableSchema(columns=narrow_stage(stage, LLMTransformStage).signature.adds)
    reply_model = reply_spec.to_pydantic_model(f"{stage.id}_reply")

    def map_row(row: Row, index: int) -> Row:
        # Per-attempt usage lands here (success or failure); the row carries its
        # summed usage out under ROW_USAGE_KEY for the driver to aggregate. Like
        # ROW_ERROR_KEY, it is an undeclared column and the output trim
        # drops it, so it never reaches stage output.
        usages: list[LlmUsage] = []
        try:
            reply = call_llm(stage.id, llm, row, reply_model=reply_model, usage_out=usages)
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
    stage: Stage,
    inputs: dict[str, pa.Table],
    ctx: RunContext,
    parallelism: int,
    positions: list[int],
) -> list[Row]:
    """Compute one raw output row per input row, in chunks of
    `stage.llm.batch_size` rows per model call, rejoining each reply to its row
    by the batch-local row number.

    Grain and order are held the same way the per-row path holds them — one
    pre-allocated result slot per input row, filled by input index, assembled in
    order — and then VERIFIED (no empty slot, count unchanged) before returning,
    so a bug in the batch path surfaces as a loud runtime error rather than a
    silently mis-grained result. Per-row independence is NOT preserved: the model
    sees a whole chunk in one prompt (see module docstring).

    Which rows arrive here is not this function's business: the handler shape
    resolves the stage-result cache and hands over only the rows that must be
    computed, with `positions` naming where each one sits in the stage's input —
    the only thing that lets a chunk's run-log detail be attributed to the rows
    it actually covers. The rows returned carry their internal columns,
    un-stripped and untrimmed — the shape assembles the stage's output frame
    from them."""
    llm = narrow_stage(stage, LLMTransformStage).llm
    assert stage.resolve_output_schema() is not None and stage.inputs[0].table_schema is not None
    batch_reply_schema = _build_batch_reply_schema(stage)

    src = inputs[stage.inputs[0].id]
    records: list[Row] = list_table_rows(src)

    results: list[Row | None] = [None] * len(records)
    process_chunk = _build_chunk_processor(
        stage, llm, batch_reply_schema, positions, ctx.run_log
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
    stage: Stage,
    llm: Any,
    batch_reply_schema: type,
    positions: list[int],
    log: RunLog | None,
) -> Callable[[int, list[Row]], list[tuple[int, Row]]]:
    """`_process_chunk` with this chunk's rows bound as the run log's detail sink."""
    # Bound HERE rather than around the fan-out: a pool worker thread starts with
    # an empty context, so the binding only reaches the model call if it happens
    # on the thread that makes it. `positions` maps the chunk's offsets back to
    # real input rows, so a chunk's prompt is never attributed to rows the cache
    # already answered and this path skipped.
    def process_chunk(start: int, chunk: list[Row]) -> list[tuple[int, Row]]:
        token = bind_detail_sink(
            log, stage.id, tuple(positions[start : start + len(chunk)])
        )
        try:
            return _process_chunk(stage, llm, batch_reply_schema, start, chunk)
        finally:
            unbind_detail_sink(token)

    return process_chunk


def _run_chunks(
    records: list[Row],
    size: int,
    parallelism: int,
    process_chunk: Callable[[int, list[Row]], list[tuple[int, Row]]],
) -> list[tuple[int, Row]]:
    """Every computed row, keyed by the position of the row it came from.
    `_process_chunk` numbers each chunk 0..N-1 for the model; `start` turns that
    number back into the row's own position."""
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


def _build_batch_reply_schema(stage: Stage) -> type:
    """The schema one chunk's reply must match: `{"results": [<item>, ...]}` where
    each item is the batch row number (the rejoin handle) plus the columns the
    signature adds. The input primary key is NOT part of it — the row number is
    the only handle."""
    reply_spec = TableSchema(columns=narrow_stage(stage, LLMTransformStage).signature.adds)
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
    stage: Stage, llm: Any, batch_reply_schema: type, start: int, chunk: list[Row]
) -> list[tuple[int, Row]]:
    """Process one chunk, THROWING THE REPLY BACK TO THE MODEL on any anomaly.

    The reply is valid only if its row numbers are exactly {0..N-1}, each once.
    Anything else — a missing number, an unknown/out-of-range number, or a
    duplicate — means the model lost track of the item↔result correspondence, so
    the whole reply is untrustworthy: we re-call the model (up to max_retries,
    telling it what was wrong). If it never returns a clean reply, EVERY row in
    the chunk is failed with ROW_ERROR_KEY — we do not keep the answers that
    happened to match, because a confused reply's other answers aren't trusted.
    Nothing is ever fabricated."""
    n = len(chunk)
    usages: list[LlmUsage] = []
    problem = "no reply produced"
    attempts = max(1, (llm.max_retries or 0) + 1)
    for attempt in range(attempts):
        task = _render_batch_task(llm.prompt_data_template, chunk, correction=problem if attempt else None)
        try:
            reply = call_llm_batch(
                stage.id, llm, instructions=llm.prompt_instructions, task=task,
                reply_schema=batch_reply_schema, usage_out=usages,
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
    """Accept a reply only if its row numbers are EXACTLY {0..N-1}, each once.
    Return (results-by-number, "") on success, or (None, <what was wrong>) so the
    caller can throw it back to the model. A count check alone is not enough — a
    duplicate + a miss can pass on length — so we check the multiset, not the
    size."""
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
    """Merge each matched reply onto its row (dropping the row-number handle) and
    tag ROW_USAGE_KEY. The chunk's usage is attributed to its first row (usage is
    per-call, not per-row); the rest carry zero so the stage total still sums."""
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
    """Fail EVERY row of a chunk whose reply never validated — its answers aren't
    trusted. One slot per row still (grain preserved); each carries ROW_ERROR_KEY."""
    total = LlmUsage.summed(usages)
    return [
        (start + offset,
         {**row, ROW_ERROR_KEY: message, ROW_USAGE_KEY: total if offset == 0 else LlmUsage()})
        for offset, row in enumerate(chunk)
    ]


def _render_batch_task(template: str, chunk: list[Row], correction: str | None = None) -> str:
    """The user-turn task for a chunk: the stage's per-row `prompt_data_template`
    rendered for each row (so batched and unbatched calls see the same per-row
    content), numbered 0..N-1, followed by the copy-the-number contract. The
    stage's row-invariant `prompt_instructions` are NOT here — they go once into
    the system prompt. On a retry the correction states what was wrong."""
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
