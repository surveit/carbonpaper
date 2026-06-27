"""
Deterministic: turn each facility's identity into the target search queries the
distillation found worked. NO LLM here — this is pure mechanism (the part the
3 research runs did the same way every time): key on the UML id, prefer
rspo.org-hosted PDFs, hit the CDM registry for biogas, fall back to press for
PROPER.
"""

from __future__ import annotations

import json

import pandas as pd


def _queries(r: pd.Series) -> list[dict]:
    name, pt, grp = r["name"], r["operator_pt"], r["parent_group"]
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


def transform(seeds: pd.DataFrame) -> pd.DataFrame:
    out = []
    for _, r in seeds.iterrows():
        out.append({
            "facility_id": r["facility_id"],
            "uml_id": r["uml_id"],
            "name": r["name"],
            "operator_pt": r["operator_pt"],
            "parent_group": r["parent_group"],
            "province": r["province"],
            "queries_json": json.dumps(_queries(r), ensure_ascii=False),
        })
    return pd.DataFrame(out)
