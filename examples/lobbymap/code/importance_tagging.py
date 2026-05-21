"""
Importance + recency tagging for evidence pieces.

For the demo this is a deterministic transform on the (mock-or-real) extracted
evidence. In production it would be an llm_transform that uses the document
and quote to assign an importance score 0-10 and a recency_weight from
published_at.

Inputs:
  evidence: extracted evidence dataframe with confidence and published_at.

Output: one row per evidence_id with importance, recency_weight, composite_weight.
"""

from __future__ import annotations

import pandas as pd
from datetime import datetime


def transform(evidence: pd.DataFrame) -> pd.DataFrame:
    df = evidence[["evidence_id", "published_at", "source_class", "confidence"]].copy()

    # Importance: in real LobbyMap, an LLM scores 0..10. Here we use a heuristic
    # over source_class so the mock pipeline produces meaningful aggregations.
    importance_by_class = {
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
    df["importance"] = df["source_class"].map(importance_by_class).fillna(3)

    # Recency: linear decay over 5 years from published_at to now.
    now = pd.Timestamp(datetime.now())
    df["published_at"] = pd.to_datetime(df["published_at"])
    age_years = (now - df["published_at"]).dt.days / 365.25
    df["recency_weight"] = ((5 - age_years) / 5).clip(lower=0).round(3)

    df["composite_weight"] = df["importance"] * df["recency_weight"]

    return df[["evidence_id", "published_at", "source_class",
               "importance", "recency_weight", "composite_weight"]]
