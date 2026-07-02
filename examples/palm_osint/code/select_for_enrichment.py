"""
Choose which facilities go into the expensive Tier-2 LLM enrichment, and shape
prompt-ready fields for the `tier2_extract` llm_transform.

The actual ROW CAP is applied by the runtime (`limit:` on this stage's YAML),
not here — this transform only DECIDES THE ORDER so that the first N rows the
limiter keeps are representative of the interesting cases: Indonesian mills
(the only ones with a structured nameplate capacity, from Trase) that are
corroborated by more than one source. That makes a 5-facility dry run exercise
real multi-source provenance instead of five single-source Brazilian rows.

This ordering is a DEMO THROTTLE, not an analytical filter — documented in the
YAML compiler_notes. In production the limit is removed and every changed /
newly-added facility (the re-enrichment queue) flows through.
"""

from __future__ import annotations

import pandas as pd


def transform(facilities: pd.DataFrame) -> pd.DataFrame:
    df = facilities.copy()

    # Deterministic "most interesting first" ordering.
    df["_has_cap"] = df["capacity_value"].notna().astype(int)
    df["_is_idn"] = (df["country"] == "Indonesia").astype(int)
    df["_multi"] = df.get("multi_source", pd.Series([False] * len(df))).astype(int)
    df = df.sort_values(
        by=["_is_idn", "_has_cap", "_multi", "facility_id"],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)

    # Compact human/LLM-readable capacity string (kept honest: says so when null).
    def _cap_str(r: pd.Series) -> str:
        if pd.isna(r["capacity_value"]):
            return "unknown (no structured capacity source for this country)"
        return f"{r['capacity_value']:.0f} {r['capacity_unit']} " \
               f"(provenance={r['capacity_provenance']}, source={r['capacity_source_url']})"

    df["capacity_summary"] = df.apply(_cap_str, axis=1)
    df = df.drop(columns=["_has_cap", "_is_idn", "_multi"])
    return df
