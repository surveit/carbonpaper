"""
Importance + recency tagging for evidence pieces.

A `python_row_function`: the runtime maps this over each evidence row, so it takes
one row (a dict) and returns one row (a dict). It cannot fan out or fan in — the
1:1 grain is guaranteed by the runtime, not by this code.

For the demo this is a deterministic transform on the (mock-or-real) extracted
evidence. In production it would be an llm_transform that uses the document and
quote to assign an importance score 0-10 and a recency_weight from published_at.

Input row:  evidence with source_class and published_at.
Output row: evidence_id, published_at, source_class, importance, recency_weight,
            composite_weight.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

# Importance heuristic over source_class. In real LobbyMap an LLM scores 0..10;
# here a lookup keeps the mock pipeline producing meaningful aggregations.
IMPORTANCE_BY_CLASS = {
    "regulatory_consultation": 9,    # detailed engagement
    "lobbying_register": 8,
    "cdp_response": 6,
    "financial_disclosure": 5,
    "corporate_media": 4,             # CEO speeches etc.
    "management_messaging": 3,
    "reliable_media": 5,
    "org_website": 2,
    "paid_climate_advertising": 2,    # high-level aspirational
}


def transform(row: dict[str, Any]) -> dict[str, Any]:
    importance = IMPORTANCE_BY_CLASS.get(row["source_class"], 3)

    # Recency: linear decay over 5 years from published_at to now.
    age_years = (pd.Timestamp(datetime.now()) - pd.to_datetime(row["published_at"])).days / 365.25
    recency_weight = round(max((5 - age_years) / 5, 0.0), 3)

    return {
        "evidence_id": row["evidence_id"],
        "published_at": row["published_at"],
        "source_class": row["source_class"],
        "importance": importance,
        "recency_weight": recency_weight,
        "composite_weight": importance * recency_weight,
    }
