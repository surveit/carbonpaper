"""Execution for the llm_transform stage type, split by `batch_size`. Grain and
order are the driver's; what is lost at > 1 is per-row INDEPENDENCE — a batched
call shows the model every row in the group. Replies rejoin by a group-local
0-based row number the runtime assigns, never the input primary key (which the
runtime does not require to exist or be unique).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pyarrow as pa
from pydantic import create_model

from app.core.agent.usage import LlmUsage
from app.core.errors import StageWideFailure
from app.models import WorkflowStage
from app.models.schema import Column, TableSchema
from app.models.stages.llm_transform import LLMTransformStage

from ..context import RunContext
from ..llm import call_llm, call_llm_batch, render_prompt

from .execution import (
    ROW_ERROR_KEY,
    ROW_USAGE_KEY,
    GroupMapper,
    Row,
    RowMapper,
    RowMapTransformHandler,
    narrow_stage,
)

# The reply field carrying a batched result's item number — the rejoin handle.
# Runtime-assigned per chunk (0-based), so it is always a small unique int the
# model just has to copy; it never touches the input primary key.
_ROW_NUMBER_FIELD = "row_number"


class LLMTransformHandler(RowMapTransformHandler):
    def __init__(self, parallelism: int = 1) -> None:
        super().__init__(
            build_llm_row_mapper, parallelism, trims_output_to_declared=True
        )

    def group_size(self, workflow_stage: WorkflowStage) -> int:
        return narrow_stage(workflow_stage, LLMTransformStage).llm.batch_size

    def make_group_mapper(
        self, workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
    ) -> GroupMapper:
        if self.group_size(workflow_stage) == 1:
            return super().make_group_mapper(workflow_stage, ctx, src)
        return build_llm_batch_mapper(workflow_stage)


# ── batch_size == 1: per-row path (grain + order + independence by construction) ──
def build_llm_row_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
) -> RowMapper:
    """A row's reply depends only on that row: neither the frame nor the row's position is read."""
    stage = narrow_stage(workflow_stage, LLMTransformStage)
    llm = stage.llm

    # What the model is asked for: the columns the signature adds, compiled to the
    # model the agent must satisfy. Its input columns are rejoined by the driver.
    reply_spec = TableSchema(columns=stage.signature.adds)
    reply_model = reply_spec.to_pydantic_model(f"{stage.id}_reply")

    def map_row(row: Row, index: int) -> Row:
        usages: list[LlmUsage] = []
        try:
            reply = call_llm(stage.id, llm, row, reply_model=reply_model, usage_out=usages)
        except StageWideFailure:
            # Not this row's failure: every remaining row would fail the same
            # way, so it stops the stage instead of tagging 5,000 rows one at a
            # time with the same message.
            raise
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
def build_llm_batch_mapper(workflow_stage: WorkflowStage) -> GroupMapper:
    """One model call per group. The driver owns the grouping, the pool, the cache and the log."""
    stage = narrow_stage(workflow_stage, LLMTransformStage)
    batch_reply_schema = _build_batch_reply_schema(stage)

    def map_group(indices: Sequence[int], rows: Sequence[Row]) -> Sequence[Row | None]:
        return _process_chunk(stage.id, stage.llm, batch_reply_schema, list(rows))

    return map_group


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
    stage_id: str, llm: Any, batch_reply_schema: type, chunk: list[Row]
) -> list[Row]:
    """A confused reply fails EVERY row of the chunk: the answers that matched are not trusted."""
    usages: list[LlmUsage] = []
    try:
        by_number, problem = _ask_until_reply_rejoins(
            stage_id, llm, batch_reply_schema, chunk, usages)
    except StageWideFailure:
        raise                       # not this chunk's failure — see map_row's supervisor
    except Exception as exc:  # noqa: BLE001 — the chunk's supervisor, mirroring the
        # per-row one: a backend that never answered fails THESE rows, not the stage.
        return _emit_failed(chunk, usages, str(exc) or type(exc).__name__)
    if by_number is None:
        return _emit_failed(chunk, usages, problem)
    return _emit_matched(chunk, by_number, usages)


def _ask_until_reply_rejoins(
    stage_id: str,
    llm: Any,
    batch_reply_schema: type,
    chunk: list[Row],
    usages: list[LlmUsage],
) -> tuple[dict[int, dict[str, Any]] | None, str]:
    """Re-asks ONLY a reply the runtime could not rejoin — the one defect no reply schema can state."""
    n = len(chunk)
    problem = "no reply produced"
    attempts = max(1, (llm.max_retries or 0) + 1)
    for attempt in range(attempts):
        task = _render_batch_task(llm.prompt_data_template, chunk, correction=problem if attempt else None)
        # A raise propagates: `call_llm_batch` has already retried the backend
        # `max_retries` times, and re-asking here would square that budget while
        # telling the model its reply was rejected — which it never made.
        reply = call_llm_batch(
            stage_id, llm, instructions=llm.prompt_instructions, task=task,
            reply_schema=batch_reply_schema, usage_out=usages,
        )
        by_number, problem = _validate_batch_reply(reply.get("results", []), n)
        if by_number is not None:
            return by_number, ""
    return None, f"batched reply invalid after {attempts} attempt(s): {problem}"


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
    chunk: list[Row], by_number: dict[int, dict[str, Any]], usages: list[LlmUsage]
) -> list[Row]:
    """Usage is per-call: the whole chunk's usage lands on its first row, the rest carry zero."""
    total = LlmUsage.summed(usages)
    out: list[Row] = []
    for offset, row in enumerate(chunk):
        reply_fields = {k: v for k, v in by_number[offset].items() if k != _ROW_NUMBER_FIELD}
        usage = total if offset == 0 else LlmUsage()
        out.append({**row, **reply_fields, ROW_USAGE_KEY: usage})
    return out


def _emit_failed(chunk: list[Row], usages: list[LlmUsage], message: str) -> list[Row]:
    total = LlmUsage.summed(usages)
    return [
        {**row, ROW_ERROR_KEY: message, ROW_USAGE_KEY: total if offset == 0 else LlmUsage()}
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
