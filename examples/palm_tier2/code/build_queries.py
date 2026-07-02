"""
Deterministic: turn each facility's identity into the target search queries the
distillation found worked. NO LLM here — this is pure mechanism (the part the
3 research runs did the same way every time): key on the UML id, prefer
rspo.org-hosted PDFs, hit the CDM registry for biogas, fall back to press for
PROPER.

Input is the Tier-1 facility universe (select_for_enrichment output: the
facilities spine cross-checked against Trase, ordered most-informative-first
and row-capped). Columns are renamed for the downstream research chain:
owner (PalmWatch "Group Name" = the operating PT) -> operator_pt,
region (province) -> province.
"""

from __future__ import annotations

import json

import pandas as pd


def _s(v) -> str:
    """A nullable cell as a plain string ('' for null) — keeps null owners from
    rendering as the literal 'None'/'nan' inside a search query."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v)


def _queries(name: str, pt: str, grp: str) -> list[dict]:
    return [
        {"target": "rspo_audit_pdf",
         "query": f'site:rspo.org "{name}" ("public summary" OR "ASA" OR recertification) filetype:pdf'},
        {"target": "rspo_certificate",
         "query": f'"{pt}" "{name}" RSPO certificate'},
        {"target": "cdm_biogas",
         "query": f'"{name}" OR "{pt}" methane OR biogas POME site:cdm.unfccc.int'},
        {"target": "proper",
         "query": f'"{pt}" PROPER menlhk'},
        {"target": "group_report",
         "query": f'{grp} sustainability report mill list "{name}"'},
    ]


def transform(selected: pd.DataFrame) -> pd.DataFrame:
    out = []
    for _, r in selected.iterrows():
        name = _s(r["name"])
        pt = _s(r.get("owner"))
        grp = _s(r.get("parent_group"))
        out.append({
            "facility_id": r["facility_id"],
            "uml_id": r["uml_id"],
            "name": name,
            "operator_pt": pt,
            "parent_group": grp,
            "province": _s(r.get("region")),
            "queries_json": json.dumps(_queries(name, pt, grp), ensure_ascii=False),
        })
    return pd.DataFrame(out)
