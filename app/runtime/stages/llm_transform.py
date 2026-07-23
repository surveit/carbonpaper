"""Row mapper for the llm_transform stage type.

Runs the stage's prompt over each input row via the LLM layer (`llm.call_llm`;
the runtime's row driver supplies bounded parallelism and reassembles results
in input order). The columns `output_schema` adds beyond the input schema are
the reply spec, compiled by `TableSchema.to_pydantic_model` into the model the
agent backend enforces — a live reply is a validated instance of it, so reply
columns arrive typed. The strictly-1:1 shape holds by construction: the mapper
returns exactly one dict per input row; `Stage` validation fixes the schema
shape at construction time."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import pandas as pd
from pydantic import create_model

from app.core.models import Stage
from app.core.models.schema import TableSchema

from ..llm import backend_status, call_llm, call_llm_batch, render_prompt
from .execution import (
    ROW_ERROR_KEY,
    Row,
    _collect_row_errors,
    _project_onto_declared_columns,
)


def make_llm_row_mapper(stage: Stage, ctx: dict[str, Any]) -> Callable[[Row], Row]:
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
    ctx.setdefault("llm_backend", {})[stage.id] = backend_status()

    def map_row(row: Row) -> Row:
        try:
            reply = call_llm(stage.id, llm, row, reply_model=reply_model)
        except Exception as exc:  # noqa: BLE001 — per-row supervisor: tag the row
            # with the ROW_ERROR_KEY sentinel so the map completes (one bad row
            # does not abort the stage); the row driver collects these off the
            # assembled frame and the runner surfaces them as error-severity
            # output issues. Falls back to the exception's type name when its
            # message is empty (e.g. a bare TimeoutError), so a message-less
            # failure still reads as a failure rather than an empty-string cell.
            return {**row, ROW_ERROR_KEY: str(exc) or type(exc).__name__}
        return {**row, **reply}

    return map_row


def run_llm_batches(
    stage: Stage,
    inputs: dict[str, pd.DataFrame],
    ctx: dict[str, Any],
    parallelism: int,
) -> pd.DataFrame:
    """Run an llm_transform stage in batches of `stage.llm.batch_size` rows per
    model call, rejoining each reply to its input row by primary key (ID in, ID
    out). One result slot per input row, filled by input index and assembled in
    order, so the stage stays strictly 1:1: a reply that drops, duplicates, or
    invents a primary key becomes a loud per-row error rather than a silent
    reshape — a fabricated reply can never fill a row."""
    llm = stage.llm
    assert llm is not None  # Stage validation: llm_transform carries llm
    input_schema = stage.inputs[0].table_schema
    assert stage.output_schema is not None and input_schema is not None
    primary_key = input_schema.primary_key
    if not primary_key:
        raise ValueError(
            f"stage {stage.id}: llm batch_size>1 needs a primary key on the input "
            "schema to rejoin batched replies by id"
        )

    # The batched reply carries each row's primary key alongside the reply spec
    # (output − input), so the runtime can rejoin by id. One call returns a list
    # of these, wrapped as {"results": [...]}.
    reply_spec = stage.output_schema.subtract(input_schema)
    pk_columns = [c for c in input_schema.columns if c.name in primary_key]
    row_schema = TableSchema(
        columns=[*pk_columns, *reply_spec.columns], primary_key=list(primary_key)
    )
    row_model = row_schema.to_pydantic_model(f"{stage.id}_batch_row")
    batch_model = create_model(f"{stage.id}_batch", results=(list[row_model], ...))  # type: ignore[valid-type]

    ctx.setdefault("llm_backend", {})[stage.id] = backend_status()

    src = inputs[stage.inputs[0].id]
    records: list[Row] = [
        {str(k): v for k, v in record.items()} for record in src.to_dict("records")
    ]
    size = llm.batch_size
    chunks = [(start, records[start : start + size]) for start in range(0, len(records), size)]

    def run_chunk(start: int, chunk: list[Row]) -> list[tuple[int, Row]]:
        return _classify_chunk(stage, llm, primary_key, reply_spec, batch_model, start, chunk)

    results: list[Row | None] = [None] * len(records)
    if parallelism > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = [pool.submit(run_chunk, start, chunk) for start, chunk in chunks]
            for future in as_completed(futures):
                for index, row in future.result():
                    results[index] = row
    else:
        for start, chunk in chunks:
            for index, row in run_chunk(start, chunk):
                results[index] = row

    df = pd.DataFrame([row for row in results])
    _collect_row_errors(df, stage, ctx)
    return _project_onto_declared_columns(df, stage, ctx)


def _classify_chunk(
    stage: Stage,
    llm: Any,
    primary_key: list[str],
    reply_spec: TableSchema,
    batch_model: type,
    start: int,
    chunk: list[Row],
) -> list[tuple[int, Row]]:
    """One batched call for `chunk`, retried on transient failure OR on an
    incomplete id set (a reply missing some of the chunk's keys), then rejoined
    by primary key. Rows still without a reply after `max_retries` are tagged
    with ROW_ERROR_KEY — never fabricated."""
    prompt = _render_batch_prompt(llm.prompt_template, chunk, primary_key, reply_spec)
    want = [_pk_key(row, primary_key) for row in chunk]
    want_set = set(want)
    by_key: dict[tuple[str, ...], Row] = {}
    attempts = max(1, (llm.max_retries or 0) + 1)
    for attempt in range(attempts):
        try:
            reply = call_llm_batch(stage.id, llm, prompt, batch_model=batch_model)
        except Exception as exc:  # noqa: BLE001 — a chunk that never returns is a loud per-row error
            if attempt + 1 < attempts:
                continue
            return [
                (start + offset, {**row, ROW_ERROR_KEY: str(exc) or type(exc).__name__})
                for offset, row in enumerate(chunk)
            ]
        for item in reply.get("results", []):
            key = _pk_key(item, primary_key)
            if key in want_set and key not in by_key:
                by_key[key] = item
        if all(key in by_key for key in want):
            break  # complete — no retry needed

    out: list[tuple[int, Row]] = []
    for offset, row in enumerate(chunk):
        item = by_key.get(_pk_key(row, primary_key))
        if item is None:
            out.append((
                start + offset,
                {**row, ROW_ERROR_KEY: "batched reply carried no result for this primary key"},
            ))
        else:
            # Keep the input row's own primary key; take only the reply columns
            # (the echoed pk was just the rejoin handle).
            reply_fields = {k: v for k, v in item.items() if k not in primary_key}
            out.append((start + offset, {**row, **reply_fields}))
    return out


def _pk_key(row: Row, primary_key: list[str]) -> tuple[str, ...]:
    """A hashable, type-normalized key for `row`'s primary key — `str` on each
    part so an input row and an echoed reply match regardless of how the model
    typed the value."""
    return tuple(str(row.get(name)) for name in primary_key)


def _render_batch_prompt(
    template: str, chunk: list[Row], primary_key: list[str], reply_spec: TableSchema
) -> str:
    """Build one prompt for a chunk: the stage's per-row template rendered for
    each row (so batched and unbatched calls see the same per-row content),
    tagged with that row's primary key, followed by the ID-in/ID-out contract."""
    key_label = "+".join(primary_key)
    items = [
        f"### item {key_label}={'+'.join(_pk_key(row, primary_key))}\n"
        + render_prompt(template, row)
        for row in chunk
    ]
    reply_fields = ", ".join(c.name for c in reply_spec.columns)
    return (
        "\n\n".join(items)
        + f"\n\n---\nThe {len(chunk)} items above each carry a primary key "
        f"({key_label}). Return exactly one result per item, each echoing its "
        f"exact primary key ({key_label}) plus the reply fields ({reply_fields}). "
        "Return one result per primary key — no more, no fewer."
    )
