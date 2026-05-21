"""
Select the LobbyMap-tracked entity set.

Input dataframes:
  forbes:        Forbes Global 2000 with 3-year-average rank.
  associations:  curated industry-association list.

Output: one entities table covering both kinds.
"""

from __future__ import annotations

import pandas as pd


# Sectors the methodology emphasizes (§3.4, line 269).
LOBBYMAP_SECTORS = {
    "Energy", "Oil & Gas Operations", "Utilities",
    "Transportation", "Aerospace & Defense", "Auto & Truck Manufacturers",
    "Chemicals", "Iron & Steel", "Construction Materials",
    "Mining", "Metals", "Food, Drink & Tobacco", "Forestry, Paper",
}


def transform(forbes: pd.DataFrame, associations: pd.DataFrame) -> pd.DataFrame:
    # Companies: top-1500 OR sector-relevant
    companies = forbes.copy()
    companies["sector_match"] = companies["industry"].isin(LOBBYMAP_SECTORS)
    companies = companies[
        (companies["rank_3y_avg"] <= 1500) | companies["sector_match"]
    ].copy()
    companies["entity_id"] = "C:" + companies["forbes_id"]
    companies["entity_kind"] = "company"
    companies["jurisdiction"] = companies["country"]
    companies["sectors"] = companies["industry"].apply(
        lambda x: [x] if isinstance(x, str) else []
    )

    company_rows = companies[
        ["entity_id", "entity_kind", "name", "jurisdiction", "sectors"]
    ]

    # Industry associations: pass through, prefix id
    assoc = associations.copy()
    assoc["entity_id"] = "A:" + assoc["association_id"]
    assoc["entity_kind"] = "association"
    assoc_rows = assoc[
        ["entity_id", "entity_kind", "name", "jurisdiction", "sectors"]
    ]

    return pd.concat([company_rows, assoc_rows], ignore_index=True)
