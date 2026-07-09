"""Handler for the llm_transform stage type."""

from __future__ import annotations

import pandas as pd

from app.models import Stage
from app.runtime.context import RunContext

from ..llm import call_llm_batch, backend_status


def handle_llm_transform(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    llm = stage.llm
    assert llm is not None  # Stage validation: llm_transform carries llm
    src = inputs[stage.inputs[0].id]
    out_rows = []

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


def _evidence_id_for(row: dict[str, object], idx: int) -> str:
    base = row.get("doc_id") or row.get("evidence_id") or "anon"
    return f"{base}#{idx}"
