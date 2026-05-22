"""
Per (member, query) cell-score aggregation with linear recency decay.

Input: reviewed evidence with final_score and published_at.
Output: one row per (entity_id, query_id) with weighted-mean cell_score.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd


# Weight at slice end = 1.0; weight at 6 months prior = 0.3; clipped below.
RECENCY_FLOOR = 0.3
RECENCY_WINDOW_DAYS = 180


def _weight(published: pd.Timestamp, ref: pd.Timestamp) -> float:
    if pd.isna(published):
        return RECENCY_FLOOR
    age_days = max((ref - published).days, 0)
    if age_days >= RECENCY_WINDOW_DAYS:
        return RECENCY_FLOOR
    return 1.0 - (1.0 - RECENCY_FLOOR) * (age_days / RECENCY_WINDOW_DAYS)


def transform(reviewed: pd.DataFrame) -> pd.DataFrame:
    if reviewed.empty:
        return pd.DataFrame(
            columns=["entity_id", "query_id", "cell_score",
                     "evidence_count", "most_recent_evidence"]
        )

    df = reviewed.copy()
    df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce")
    df = df.dropna(subset=["final_score"])

    ref = df["published_at"].max()
    df["weight"] = df["published_at"].apply(lambda p: _weight(p, ref))
    df["weighted"] = df["final_score"].astype(float) * df["weight"]

    grouped = df.groupby(["entity_id", "query_id"], dropna=False)
    out = grouped.agg(
        cell_score=("weighted", "sum"),
        weight_sum=("weight", "sum"),
        evidence_count=("evidence_id", "count"),
        most_recent_evidence=("published_at", "max"),
    ).reset_index()
    out["cell_score"] = np.where(
        out["weight_sum"] > 0,
        out["cell_score"] / out["weight_sum"],
        np.nan,
    )
    out["most_recent_evidence"] = out["most_recent_evidence"].dt.date.astype(str)
    return out[["entity_id", "query_id", "cell_score",
                "evidence_count", "most_recent_evidence"]]
