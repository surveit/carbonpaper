"""
Apply adversarial-verification verdicts to reviewed Tier-2 feature claims —
a faithful re-expression of food-bev-facility-osint's
`enrichment_merge._apply_verdicts`:

  - asserted present=true  & supported=true   -> KEEP as verified
  - asserted present=true  & supported=false  -> DROP (refuted; never published)
  - asserted present=false                    -> KEEP as documented negative
  - asserted present=unknown                  -> KEEP as a flagged data gap (low conf)

Rows the human reviewer rejected upstream have already been dropped by the
human_review_queue. What remains here gets the verify verdict applied. The
cardinal rule: a present=true feature with no supporting verdict is never
silently accepted — it is dropped, not demoted-and-shown as fact.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def transform(reviewed: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, r in reviewed.iterrows():
        present = str(r.get("asserted_present") or "unknown").strip().lower()
        supported = _truthy(r.get("supported"))
        confidence = r.get("confidence") or "low"

        if present == "true":
            if supported:
                status, final_conf = "verified", confidence
            else:
                # Refuted / unverified positive — drop it. Do not publish.
                continue
        elif present == "false":
            status, final_conf = "documented_negative", confidence
        else:
            status, final_conf = "unknown_gap", "low"

        rows.append({
            "facility_id": r.get("facility_id"),
            "feature": r.get("feature"),
            "asserted_present": present,
            "supported": bool(supported),
            "final_confidence": final_conf,
            "enrichment_status": status,
            "detail": r.get("detail"),
            "evidence_urls": r.get("evidence_urls"),
            "verdict_reason": r.get("verdict_reason"),
        })

    cols = ["facility_id", "feature", "asserted_present", "supported",
            "final_confidence", "enrichment_status", "detail",
            "evidence_urls", "verdict_reason"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows)[cols]
