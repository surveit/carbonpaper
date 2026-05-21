"""
Aggregate scored evidence into (entity × source × query) cells.

Joins reviewed scores with importance/recency weights, then computes a
weighted-mean cell_score and weighted-sum cell_intensity per cell.

Inputs:
  reviewed:  extreme_score_review output (final_score per evidence × benchmark).
  weights:   importance_tagging output (composite_weight per evidence).

Output: one row per (entity_id, source_class, query_id) cell.
"""

from __future__ import annotations

import pandas as pd


def transform(reviewed: pd.DataFrame, weights: pd.DataFrame) -> pd.DataFrame:
    df = reviewed.merge(weights, on="evidence_id", how="inner")
    # Drop rows where final_score is null (precedence_failure cases).
    df = df.dropna(subset=["final_score"])
    if df.empty:
        return pd.DataFrame(columns=[
            "entity_id", "source_class", "query_id",
            "cell_score", "cell_intensity", "evidence_count", "latest_evidence_at",
        ])

    df["weighted_score"] = df["final_score"] * df["composite_weight"]

    grouped = df.groupby(["entity_id", "source_class", "query_id"], dropna=False).agg(
        weighted_sum=("weighted_score", "sum"),
        weight_sum=("composite_weight", "sum"),
        cell_intensity=("composite_weight", "sum"),
        evidence_count=("evidence_id", "count"),
        latest_evidence_at=("published_at", "max"),
    ).reset_index()

    grouped["cell_score"] = grouped["weighted_sum"] / grouped["weight_sum"]
    return grouped[[
        "entity_id", "source_class", "query_id",
        "cell_score", "cell_intensity", "evidence_count", "latest_evidence_at",
    ]]
