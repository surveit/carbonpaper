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

from typing import Any, Callable

from app.core.models import Stage

from ..llm import backend_status, call_llm
from .execution import Row


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
        except Exception as exc:  # noqa: BLE001 — per-row supervisor: any backend
            # failure (agent error, timeout, validation exhaustion, …) is
            # recorded as _error so one bad row can't abort the stage;
            # surfaced, not swallowed.
            return {**row, "_error": str(exc)}
        return {**row, **reply}

    return map_row
