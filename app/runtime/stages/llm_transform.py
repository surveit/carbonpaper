"""Row mapper for the llm_transform stage type.

Runs the stage's prompt over each input row via the LLM layer (`llm.call_llm`;
the runtime's row driver supplies bounded parallelism and reassembles results
in input order). The columns `output_schema` adds beyond the input schema are
the reply spec: rendered by `TableSchema.to_prompt` and appended to the prompt
so the model is told exactly which keys to return. The strictly-1:1 shape holds
by construction: the mapper returns exactly one dict per input row whatever
shape the model replies with (a non-dict reply — prose, scalar, or JSON list —
is kept whole in `_raw`, never parsed into rows); `Stage` validation fixes the
schema shape at construction time."""

from __future__ import annotations

from typing import Any, Callable

from app.models import Stage

from ..llm import backend_status, call_llm
from .execution import Row


def make_llm_row_mapper(stage: Stage, ctx: dict[str, Any]) -> Callable[[Row], Row]:
    llm = stage.llm
    assert llm is not None  # Stage validation: llm_transform carries llm

    # Append the derived reply spec (output_schema − input_schema) to the prompt
    # so the model is told exactly which keys to return. Stage validation
    # guarantees an llm_transform is 1:1 (both schemas present, output ⊇ input),
    # so subtract never throws here.
    input_schema = stage.inputs[0].table_schema
    assert stage.output_schema is not None and input_schema is not None
    reply_spec = stage.output_schema.subtract(input_schema)
    llm_config = llm.model_copy(
        update={"prompt_template": f"{llm.prompt_template}\n\n{reply_spec.to_prompt()}"}
    )

    # Record which backend handled this stage so the UI/manifest can label it.
    ctx.setdefault("llm_backend", {})[stage.id] = backend_status()

    def map_row(row: Row) -> Row:
        try:
            reply = call_llm(stage.id, llm_config, row)
        except Exception as exc:  # noqa: BLE001 — per-row supervisor: any backend
            # failure (network, subprocess, parse, …) is recorded as _error so
            # one bad row can't abort the stage; surfaced, not swallowed.
            return {**row, "_error": str(exc)}
        if isinstance(reply, dict):
            return {**row, **reply}
        # Any non-dict reply (prose, a scalar, a JSON list) is a value, not rows:
        # kept whole in _raw, and this row stays one row. The declared reply
        # columns are then absent, which output-schema validation surfaces.
        return {**row, "_raw": str(reply)}

    return map_row
