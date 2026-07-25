"""Execution for the llm_transform stage type — two clearly separate paths, one
per `batch_size`, dispatched by `LLMTransformHandler`:

- `make_llm_row_mapper` (batch_size == 1): the per-row mapper the runtime drives
  one row at a time. Grain, order, AND per-row independence hold by construction
  — the mapper never sees the frame, so a row's output depends only on its own
  input.
- `run_llm_batches` (batch_size > 1): packs N rows into one model call to
  amortize the prompt/harness overhead. Grain and order still hold (one
  pre-allocated slot per input row, filled by input index, and verified before
  returning). Per-row INDEPENDENCE does NOT: the model sees every row in a chunk
  in one prompt, so a row's answer can in principle be influenced by its
  batch-mates. That is the semantic price of batching, and the reason it is a
  separate function rather than a mode folded into the per-row path.

Replies are rejoined to rows by a batch-local ROW NUMBER the runtime assigns
(0-based, per chunk) — never the input primary key. The number is a tiny int we
control, so the model can't mangle it and the rejoin does not depend on the
primary key existing or being unique (the runtime enforces neither). The columns
`output_schema` adds beyond the input schema are the reply spec, compiled by
`TableSchema.to_pydantic_model` into the per-item reply schema.

Both paths participate in the stage-result cache (app.services.stage_cache),
keyed the same way human_review_queue's decisions are — stage-definition
fingerprint + per-row input fingerprint — so a row this stage already generated
a reply for is never re-asked of the model on a later run: pinning the model's
answer is both a cost win and what keeps a downstream human_review_queue's rows
stable across runs (a fresh LLM re-roll would otherwise fingerprint as a
different row and re-queue it). `_resolve_llm_cache` decides, once per stage
execution, whether this stage's rows participate at all — never for a run with
no project scope (a subset/preview run).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd
from pydantic import create_model

from app.core.agent.usage import LlmUsage
from app.core.persistence import JsonDict
from app.models import Stage
from app.models.schema import Column, TableSchema
from app.services.stage_cache import (
    ReadOnlyStageCache,
    StageCache,
    StageCacheEntry,
    build_cache_id,
    compute_row_fingerprint,
    to_json_safe_row,
)

from ..context import RunContext
from ..llm import backend_status, call_llm, call_llm_batch, render_prompt
from .execution import (
    ROW_ERROR_KEY,
    ROW_USAGE_KEY,
    Row,
    _collect_row_errors,
    _collect_row_usage,
    _project_onto_declared_columns,
)

# The reply field carrying a batched result's item number — the rejoin handle.
# Runtime-assigned per chunk (0-based), so it is always a small unique int the
# model just has to copy; it never touches the input primary key.
_ROW_NUMBER_FIELD = "row_number"


@dataclass(frozen=True)
class _LlmCache:
    """One llm_transform stage's cache participation for one execution: the
    project + stage-definition fingerprint its cache keys are scoped under, and
    the accessor this run was granted (StageCache read+write for a production
    run, ReadOnlyStageCache read-only for a non-production one). Built once per
    stage execution (`_resolve_llm_cache`), never per row — the fingerprint is
    the same for every row of this stage's execution; only the per-row input
    fingerprint varies."""

    project: str
    stage_id: str
    stage_fp: str
    accessor: ReadOnlyStageCache


def _resolve_llm_cache(stage: Stage, ctx: RunContext) -> _LlmCache | None:
    """None when this stage's rows must not touch the cache at all this
    execution — the run carries no project scope (a subset/preview run:
    `ctx.identity`/`ctx.stage_cache` are None together, see
    RunContext.__post_init__). Otherwise the handle every row of this
    execution reads and writes its cache entry through."""
    if ctx.identity is None or ctx.stage_cache is None:
        return None
    return _LlmCache(
        project=ctx.identity.project, stage_id=stage.id,
        stage_fp=stage.compute_definition_fingerprint(), accessor=ctx.stage_cache,
    )


def _build_llm_cache_entry(
    cache: _LlmCache, ctx: RunContext, row: Row, reply: JsonDict
) -> StageCacheEntry:
    """The cache entry recording that `stage.id` produced `reply` (its added
    columns only, never the whole merged row) for this row. `ctx.identity` is
    guaranteed set here: it co-varies with `ctx.stage_cache` (RunContext.
    __post_init__), and `cache` exists only when both were granted
    (`_resolve_llm_cache`)."""
    assert ctx.identity is not None
    row_fp = compute_row_fingerprint(row)
    return StageCacheEntry(
        id=build_cache_id(cache.project, cache.stage_id, cache.stage_fp, row_fp),
        project=cache.project, stage_id=cache.stage_id, stage_fingerprint=cache.stage_fp,
        input_fingerprint=row_fp, source_run_id=ctx.identity.run_id,
        frozen_input=to_json_safe_row(row), human=None, llm_output=to_json_safe_row(reply),
    )


# ── batch_size == 1: per-row path (grain + order + independence by construction) ──
def make_llm_row_mapper(stage: Stage, ctx: RunContext) -> Callable[[Row], Row]:
    llm = stage.llm
    assert llm is not None  # Stage validation: llm_transform carries llm

    # The reply spec (output_schema − input_schema), compiled to the model the
    # agent must satisfy. Stage validation guarantees an llm_transform is 1:1
    # (both schemas present, output ⊇ input), so subtract never throws here.
    input_schema = stage.inputs[0].table_schema
    assert stage.output_schema is not None and input_schema is not None
    reply_spec = stage.output_schema.subtract(input_schema)
    reply_model = reply_spec.to_pydantic_model(f"{stage.id}_reply")

    # Record which backend handled this stage so the UI/manifest can label it.
    ctx.llm_backend[stage.id] = backend_status()

    cache = _resolve_llm_cache(stage, ctx)

    def map_row(row: Row) -> Row:
        # Per-attempt usage lands here (success or failure); the row carries its
        # summed usage out under ROW_USAGE_KEY for the driver to aggregate. Like
        # ROW_ERROR_KEY, it is an undeclared column and the output projection
        # drops it, so it never reaches stage output.
        usages: list[LlmUsage] = []

        # A cache hit skips the model call entirely.
        if cache is not None:
            hit = cache.accessor.get(
                cache.project, cache.stage_id, cache.stage_fp, compute_row_fingerprint(row)
            )
            if hit is not None:
                assert hit.llm_output is not None  # an llm_transform entry always carries one
                return {**row, **hit.llm_output, ROW_USAGE_KEY: LlmUsage()}

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

        # A freshly computed reply is written back through the seam only when
        # this run's accessor is write-capable (StageCache — a production run;
        # ReadOnlyStageCache structurally has no `put`, so a non-production run
        # reuses a hit but never records one). Failed rows never reach here —
        # only a genuine model answer is ever cached.
        if cache is not None and isinstance(cache.accessor, StageCache):
            cache.accessor.put(_build_llm_cache_entry(cache, ctx, row, reply))

        return {**row, **reply, ROW_USAGE_KEY: LlmUsage.summed(usages)}

    return map_row


# ── batch_size > 1: batched path (grain + order preserved and VERIFIED) ──
def run_llm_batches(
    stage: Stage,
    inputs: dict[str, pd.DataFrame],
    ctx: RunContext,
    parallelism: int,
) -> pd.DataFrame:
    """Run an llm_transform stage in chunks of `stage.llm.batch_size` rows per
    model call, rejoining each reply to its row by the batch-local row number.

    Grain and order are held the same way the per-row path holds them — one
    pre-allocated result slot per input row, filled by input index, assembled in
    order — and then VERIFIED (no empty slot, count unchanged) before returning,
    so a bug in the batch path surfaces as a loud runtime error rather than a
    silently mis-grained frame. Per-row independence is NOT preserved: the model
    sees a whole chunk in one prompt (see module docstring).

    Cache participation (module docstring) happens BEFORE chunking: every
    row's cache is checked first (`_fill_cached_rows`), and only rows that miss
    (or every row, if caching does not apply this execution — see
    `_resolve_llm_cache`) are packed into chunks and sent to the model at all —
    a chunk of size `batch_size` may end up smaller than authored, or the whole
    batched call may be skipped, when enough of its rows already have a cached
    reply. Freshly computed rows are written back (`_write_freshly_computed_rows`)
    once every slot is filled and the grain/order guarantee is verified."""
    llm = stage.llm
    assert llm is not None  # Stage validation: llm_transform carries llm
    input_schema = stage.inputs[0].table_schema
    assert stage.output_schema is not None and input_schema is not None
    reply_column_names = [c.name for c in stage.output_schema.subtract(input_schema).columns]
    batch_reply_schema = _build_batch_reply_schema(stage)
    ctx.llm_backend[stage.id] = backend_status()
    cache = _resolve_llm_cache(stage, ctx)

    src = inputs[stage.inputs[0].id]
    records: list[Row] = [
        {str(k): v for k, v in record.items()} for record in src.to_dict("records")
    ]

    results: list[Row | None] = [None] * len(records)
    missing_indices = _fill_cached_rows(cache, ctx, records, results)

    size = llm.batch_size
    chunks: list[tuple[list[int], list[Row]]] = []
    for start in range(0, len(missing_indices), size):
        index_chunk = missing_indices[start : start + size]
        chunks.append((index_chunk, [records[i] for i in index_chunk]))

    if parallelism > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = [
                pool.submit(_process_chunk, stage, llm, batch_reply_schema, index_chunk, chunk)
                for index_chunk, chunk in chunks
            ]
            for future in as_completed(futures):
                for index, row in future.result():
                    results[index] = row
    else:
        for index_chunk, chunk in chunks:
            for index, row in _process_chunk(stage, llm, batch_reply_schema, index_chunk, chunk):
                results[index] = row

    # Grain + order guarantee, verified not assumed: exactly one row per input,
    # every slot filled, in input order. A gap here is a batch-driver bug, raised
    # loudly — never a returned frame with the wrong row count.
    if len(results) != len(records) or any(row is None for row in results):
        raise RuntimeError(
            f"stage {stage.id}: batched execution did not produce exactly one row "
            f"per input row ({len(records)} in)"
        )

    _write_freshly_computed_rows(cache, ctx, records, results, missing_indices, reply_column_names)

    df = pd.DataFrame(results)
    _collect_row_errors(df, stage, ctx)
    _collect_row_usage(df, stage, ctx)
    return _project_onto_declared_columns(df, stage, ctx)


def _fill_cached_rows(
    cache: _LlmCache | None, ctx: RunContext, records: list[Row], results: list[Row | None]
) -> list[int]:
    """Fill `results` in place from cached entries (bulk-fetched once via
    `find_entries`, the same shape human_review_queue looks its own decisions
    up through) and return the positions left unfilled — the rows this
    execution must actually send to the model, in original input order. Every
    position is "missing" when caching does not apply to this stage execution
    at all (`cache is None`)."""
    if cache is None:
        return list(range(len(records)))
    entries_by_fingerprint = {
        entry.input_fingerprint: entry
        for entry in cache.accessor.find_entries(cache.project, cache.stage_id, cache.stage_fp)
    }
    missing: list[int] = []
    for index, record in enumerate(records):
        entry = entries_by_fingerprint.get(compute_row_fingerprint(record))
        if entry is None:
            missing.append(index)
            continue
        assert entry.llm_output is not None  # an llm_transform entry always carries one
        results[index] = {**record, **entry.llm_output, ROW_USAGE_KEY: LlmUsage()}
    return missing


def _write_freshly_computed_rows(
    cache: _LlmCache | None,
    ctx: RunContext,
    records: list[Row],
    results: list[Row | None],
    missing_indices: list[int],
    reply_column_names: list[str],
) -> None:
    """Write back every freshly computed row's reply (never a cache hit's own
    replayed reply — re-recording it would be a no-op at best) through the
    seam, once every slot in `results` is verified filled. A row this run
    failed to generate (carries ROW_ERROR_KEY) is never cached — a confused or
    failed reply's answers aren't trusted (see `_process_chunk`'s docstring).
    A no-op when caching does not apply this execution or this run's accessor
    is read-only (ReadOnlyStageCache has no `put` — the capability is
    structurally absent, not withheld by a flag)."""
    if cache is None or not isinstance(cache.accessor, StageCache):
        return
    for index in missing_indices:
        row = results[index]
        assert row is not None  # verified filled by the caller before this runs
        if row.get(ROW_ERROR_KEY) is not None:
            continue
        reply = {name: row[name] for name in reply_column_names}
        cache.accessor.put(_build_llm_cache_entry(cache, ctx, records[index], reply))


def _build_batch_reply_schema(stage: Stage) -> type:
    """The schema one chunk's reply must match: `{"results": [<item>, ...]}` where
    each item is the batch row number (the rejoin handle) plus the reply spec
    (output − input). The input primary key is NOT part of it — the row number is
    the only handle."""
    input_schema = stage.inputs[0].table_schema
    assert stage.output_schema is not None and input_schema is not None
    reply_spec = stage.output_schema.subtract(input_schema)
    number_column = Column(
        name=_ROW_NUMBER_FIELD, type="int", nullable=False,
        description=(
            "The item number shown in the prompt (0-based). Copy it exactly onto "
            "this result so it is matched back to its item."
        ),
    )
    item_schema = TableSchema(columns=[number_column, *reply_spec.columns], primary_key=None)
    item_reply = item_schema.to_pydantic_model(f"{stage.id}_batch_item")
    return create_model(f"{stage.id}_batch", results=(list[item_reply], ...))  # type: ignore[valid-type]


def _process_chunk(
    stage: Stage, llm: Any, batch_reply_schema: type, index_chunk: list[int], chunk: list[Row]
) -> list[tuple[int, Row]]:
    """Process one chunk, THROWING THE REPLY BACK TO THE MODEL on any anomaly.

    The reply is valid only if its row numbers are exactly {0..N-1}, each once.
    Anything else — a missing number, an unknown/out-of-range number, or a
    duplicate — means the model lost track of the item↔result correspondence, so
    the whole reply is untrustworthy: we re-call the model (up to max_retries,
    telling it what was wrong). If it never returns a clean reply, EVERY row in
    the chunk is failed with ROW_ERROR_KEY — we do not keep the answers that
    happened to match, because a confused reply's other answers aren't trusted.
    Nothing is ever fabricated.

    `index_chunk[offset]` is `chunk[offset]`'s position in the STAGE'S OWN
    input records — not necessarily `start + offset`: when caching serves some
    rows directly (`_fill_cached_rows`), the rows sent to the model are only
    the ones that missed, so a chunk's original positions may be
    non-contiguous."""
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
            return _emit_matched(index_chunk, chunk, by_number, usages)
        # anomaly → loop and re-call the model (throw back)
    return _emit_failed(index_chunk, chunk, usages,
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
    index_chunk: list[int], chunk: list[Row], by_number: dict[int, dict[str, Any]],
    usages: list[LlmUsage],
) -> list[tuple[int, Row]]:
    """Merge each matched reply onto its row (dropping the row-number handle) and
    tag ROW_USAGE_KEY. The chunk's usage is attributed to its first row (usage is
    per-call, not per-row); the rest carry zero so the stage total still sums.
    `index_chunk[offset]` is `chunk[offset]`'s own original position — see
    `_process_chunk`'s docstring for why that isn't always `start + offset`."""
    total = LlmUsage.summed(usages)
    out: list[tuple[int, Row]] = []
    for offset, row in enumerate(chunk):
        reply_fields = {k: v for k, v in by_number[offset].items() if k != _ROW_NUMBER_FIELD}
        usage = total if offset == 0 else LlmUsage()
        out.append((index_chunk[offset], {**row, **reply_fields, ROW_USAGE_KEY: usage}))
    return out


def _emit_failed(
    index_chunk: list[int], chunk: list[Row], usages: list[LlmUsage], message: str
) -> list[tuple[int, Row]]:
    """Fail EVERY row of a chunk whose reply never validated — its answers aren't
    trusted. One slot per row still (grain preserved); each carries ROW_ERROR_KEY."""
    total = LlmUsage.summed(usages)
    return [
        (index_chunk[offset],
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
