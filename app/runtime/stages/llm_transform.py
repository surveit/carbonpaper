"""Handler for the llm_transform stage type.

Runs the stage's prompt over each input row via the LLM layer
(`llm.call_llm_batch`) and assembles the declared output columns. The columns
`output_schema` adds beyond the input schema are the reply spec: rendered by
`TableSchema.to_prompt` and appended to the prompt so the model is told exactly
which keys to return. The strictly-1:1 shape is enforced by `Stage` validation
(construction time), not in this handler."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.models import LlmTransformStage

from ..llm import call_llm_batch, backend_status


def handle_llm_transform(stage: LlmTransformStage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    llm = stage.llm
    src = inputs[stage.inputs[0].id]
    out_rows = []

    # Append the derived reply spec (output_schema − input_schema) to the prompt
    # so the model is told exactly which keys to return. This is the only thing
    # llm_transform adds over a plain LLM call; the call machinery is untouched.
    input_schema = stage.inputs[0].table_schema
    # Stage validation guarantees an llm_transform is 1:1 (output_schema required
    # and present, input schema present, output ⊇ input), so subtract never
    # throws here.
    assert input_schema is not None
    reply_spec = stage.output_schema.subtract(input_schema)
    llm = llm.model_copy(
        update={"prompt_template": f"{llm.prompt_template}\n\n{reply_spec.to_prompt()}"}
    )

    # Record which backend handled this stage so the UI/manifest can label it.
    ctx.setdefault("llm_backend", {})[stage.id] = backend_status()

    # str(k) is a no-op for real data (parquet/CSV column labels are strings);
    # it pins the key type down from pandas' Hashable.
    row_dicts = [{str(k): v for k, v in row.items()} for _, row in src.iterrows()]
    results = call_llm_batch(stage.id, llm, row_dicts)

    for row_dict, result in zip(row_dicts, results):
        if isinstance(result, list):
            for idx, item in enumerate(result):
                merged = {**row_dict, **(item if isinstance(item, dict) else {"_value": item})}
                merged["evidence_id"] = _evidence_id_for(row_dict, idx)
                out_rows.append(merged)
        elif isinstance(result, dict):
            merged = {**row_dict, **result}
            out_rows.append(merged)
        else:
            out_rows.append({**row_dict, "_raw": str(result)})

    df = pd.DataFrame(out_rows)
    # Keep only columns declared in output_schema, preserving order, plus any
    # passthrough columns that schema declared with source: passthrough.
    declared = [c.name for c in stage.output_schema.columns] if stage.output_schema else []
    if declared:
        keep = [c for c in declared if c in df.columns]
        # Also keep stable id columns commonly used downstream
        for must_keep in ["evidence_id", "doc_id", "entity_id", "source_class", "published_at",
                          "benchmark_id", "query_id"]:
            if must_keep in df.columns and must_keep not in keep:
                keep.append(must_keep)
        df = df[keep]
    return df


def _evidence_id_for(row: dict[str, Any], idx: int) -> str:
    base = row.get("doc_id") or row.get("evidence_id") or "anon"
    return f"{base}#{idx}"
