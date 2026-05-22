"""
Rank drift_brief output by notability_score, attach stance/topical drift counts.
"""

from __future__ import annotations

import json

import pandas as pd


def _safe_len(v) -> int:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    if isinstance(v, list):
        return len(v)
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return len(parsed) if isinstance(parsed, list) else 0
        except json.JSONDecodeError:
            return 0
    return 0


def transform(drift_brief: pd.DataFrame) -> pd.DataFrame:
    if drift_brief.empty:
        return pd.DataFrame(columns=[
            "rank", "entity_id", "name", "party", "state", "chamber",
            "notability_score", "headline", "stance_drift_count",
            "topical_drift_count", "story_hypothesis",
        ])

    df = drift_brief.copy()
    df["notability_score"] = pd.to_numeric(df["notability_score"], errors="coerce").fillna(0).astype(int)
    df["stance_drift_count"] = df["stance_drift"].apply(_safe_len)
    df["topical_drift_count"] = df["topical_drift"].apply(_safe_len)

    df = df.sort_values(
        ["notability_score", "stance_drift_count"],
        ascending=[False, False],
    ).reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)

    keep = ["rank", "entity_id", "name", "party", "state", "chamber",
            "notability_score", "headline", "stance_drift_count",
            "topical_drift_count", "story_hypothesis"]
    return df[keep]
