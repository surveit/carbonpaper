"""
Deterministic: collapse all per-doc extractions for a facility into ONE row, so
the ADJUDICATE llm can reconcile across documents in a single call (llm_transform
runs row-by-row, so cross-doc reconciliation needs the docs pre-grouped). NO LLM.
"""

from __future__ import annotations

import json

import pandas as pd


def _coerce(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return []
    if hasattr(v, "tolist"):
        return list(v.tolist())
    return v or []


def transform(extracted: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if extracted.empty:
        return pd.DataFrame(columns=["facility_id", "name", "n_docs", "docs_json"])
    for fid, g in extracted.groupby("facility_id"):
        docs = []
        for _, r in g.iterrows():
            docs.append({
                "doc_type": r.get("doc_type"),
                "url": r.get("url"),
                "fields": _coerce(r.get("fields")),
            })
        rows.append({
            "facility_id": fid,
            "name": g.iloc[0].get("name"),
            "n_docs": int(len(g)),
            "docs_json": json.dumps(docs, ensure_ascii=False),
        })
    return pd.DataFrame(rows)
