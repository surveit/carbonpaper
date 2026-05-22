"""
For each member, build a single row pairing their press releases across
the two windows into prompt-ready text blocks (titles + truncated bodies).

Truncation:
  - keep up to MAX_PER_WINDOW most recent releases
  - body trimmed to BODY_MAX chars
This keeps the per-row prompt small enough to fit Haiku comfortably while
preserving the journalistic content the LLM needs to detect drift.
"""

from __future__ import annotations

import json

import pandas as pd


MAX_PER_WINDOW = 30
BODY_MAX = 1500


def _block(rows: pd.DataFrame) -> tuple[str, dict[str, str]]:
    """Return (formatted_block, url_map). rows is sorted ascending by date."""
    if rows.empty:
        return "(no releases)", {}
    parts: list[str] = []
    url_map: dict[str, str] = {}
    for _, r in rows.iterrows():
        title = (r.get("title") or "").strip().replace("\n", " ")[:200]
        body = (r.get("body") or "").strip().replace("\r", " ")[:BODY_MAX]
        date = str(r.get("published_at") or "")[:10]
        doc_id = r.get("doc_id", "")
        url = r.get("url", "")
        url_map[doc_id] = url
        parts.append(f"--- {doc_id} ({date}) ---\nTITLE: {title}\nBODY: {body}")
    return "\n\n".join(parts), url_map


def transform(
    members_universe: pd.DataFrame,
    press_window_early: pd.DataFrame,
    press_window_recent: pd.DataFrame,
) -> pd.DataFrame:
    early_by_member = {
        eid: g.sort_values("published_at").head(MAX_PER_WINDOW)
        for eid, g in press_window_early.groupby("entity_id")
    }
    recent_by_member = {
        eid: g.sort_values("published_at").head(MAX_PER_WINDOW)
        for eid, g in press_window_recent.groupby("entity_id")
    }

    rows = []
    for _, m in members_universe.iterrows():
        eid = m["entity_id"]
        early_rows = early_by_member.get(eid, pd.DataFrame())
        recent_rows = recent_by_member.get(eid, pd.DataFrame())
        if early_rows.empty or recent_rows.empty:
            continue
        early_block, early_urls = _block(early_rows)
        recent_block, recent_urls = _block(recent_rows)
        rows.append({
            "entity_id": eid,
            "name": m["name"],
            "party": m["party"],
            "state": m["state"],
            "chamber": m["chamber"],
            "early_block": early_block,
            "recent_block": recent_block,
            "early_count": int(len(early_rows)),
            "recent_count": int(len(recent_rows)),
            "early_url_map": json.dumps(early_urls),
            "recent_url_map": json.dumps(recent_urls),
        })
    return pd.DataFrame(rows)
