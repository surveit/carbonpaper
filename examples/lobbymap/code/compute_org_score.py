"""
Compute organisation-level Org Score and Engagement Intensity from cells.

Inputs:
  cells:        per-(entity_id, source_class, query_id) cell scores and intensities.
  cell_weights: configuration table mapping (sector, source_class, query_id) to a weight.
                Sector weighting per §4.7 — automotive emphasises Q11, etc.
  entities:     tracked_entities, used to look up sector for weighting.

Output: one row per entity_id with org_score, engagement_intensity, and
org_score_band per the §2.3 thresholds.
"""

from __future__ import annotations

import pandas as pd

# §2.3 anchors: org_score band thresholds
ORG_BAND_THRESHOLDS = [(75, "+"), (50, ""), (0, "-")]

# §2.3 anchors: engagement_intensity gates
INTENSITY_GATES = [(25, "high"), (12, "medium"), (5, "low"), (0, "minimal")]

# Methodology requires intensity >= 5 for org_score to be meaningful.
INTENSITY_FLOOR_FOR_SCORE = 5.0


def transform(
    cells: pd.DataFrame,
    cell_weights: pd.DataFrame,
    entities: pd.DataFrame,
) -> pd.DataFrame:
    # Attach sector from entities, then weight from cell_weights.
    df = cells.merge(
        entities[["entity_id", "sectors"]], on="entity_id", how="left"
    )
    # Sectors is a list — explode to one row per sector then weight, then mean.
    df = df.explode("sectors").rename(columns={"sectors": "sector"})

    df = df.merge(
        cell_weights, on=["sector", "source_class", "query_id"], how="left"
    )
    df["weight"] = df["weight"].fillna(0.0)

    # Renormalize weights within entity so dropped (NS / NA / >5y) cells don't
    # collapse the score. Effective weight = configured weight / sum-of-weights-
    # for-cells-with-data within this entity.
    df["has_data"] = df["cell_score"].notna()
    weight_sum = (
        df[df["has_data"]]
        .groupby("entity_id")["weight"]
        .sum()
        .rename("entity_weight_sum")
        .reset_index()
    )
    df = df.merge(weight_sum, on="entity_id", how="left")
    df["effective_weight"] = (df["weight"] / df["entity_weight_sum"]).where(
        df["entity_weight_sum"] > 0, 0.0
    )

    # Org Score: weighted mean of cell_score, scaled to 0–100 with -2→0, +2→100.
    scored = df[df["has_data"]].copy()
    scored["weighted"] = scored["cell_score"] * scored["effective_weight"]

    org = scored.groupby("entity_id").agg(
        raw_score=("weighted", "sum"),
        engagement_intensity=("cell_intensity", "sum"),
    ).reset_index()
    org["org_score"] = ((org["raw_score"] + 2.0) / 4.0 * 100.0).clip(0, 100)

    # Apply intensity gate
    low_intensity = org["engagement_intensity"] < INTENSITY_FLOOR_FOR_SCORE
    org.loc[low_intensity, "org_score"] = pd.NA

    # Band assignment
    org["org_score_band"] = org["org_score"].apply(_band_letter)
    org["intensity_label"] = org["engagement_intensity"].apply(_intensity_label)

    return org[
        [
            "entity_id", "org_score", "org_score_band",
            "engagement_intensity", "intensity_label",
        ]
    ]


def _band_letter(score: float | pd._libs.missing.NAType) -> str | pd._libs.missing.NAType:
    if pd.isna(score):
        return pd.NA
    if score >= 95: return "A+"
    if score >= 85: return "A"
    if score >= 75: return "A-"
    if score >= 65: return "B"
    if score >= 50: return "C"
    if score >= 35: return "D"
    if score >= 25: return "E"
    return "F"


def _intensity_label(intensity: float) -> str:
    for threshold, label in INTENSITY_GATES:
        if intensity >= threshold:
            return label
    return "minimal"
