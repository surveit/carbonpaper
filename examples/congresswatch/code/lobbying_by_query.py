"""
Aggregate lobbying filings per policy query, joining on issue_code overlap.

For each policy query (from policy_queries.csv) we:
  - find filings whose issue_codes overlap with the query's issue_codes
  - sum income (best-effort parse from LDA free-form strings)
  - rank top clients and top filer organizations by filing count
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

import pandas as pd


_NUM = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")


def _parse_money(s) -> float:
    """Best-effort: take the largest dollar number from the value. LDA
    filings store income as either a numeric string like '20000.00', a
    free-text range like 'Less than $5,000', or blank. Pandas may have
    coerced the column to float at read time. Returns 0.0 if nothing
    parses."""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        if pd.isna(s):
            return 0.0
        return float(s)
    if not isinstance(s, str) or not s.strip():
        return 0.0
    matches = _NUM.findall(s)
    if not matches:
        return 0.0
    try:
        return max(float(m.replace(",", "")) for m in matches)
    except ValueError:
        return 0.0


def _split_codes(s: str | None) -> set[str]:
    if not s:
        return set()
    return {c.strip() for c in s.split(";") if c.strip()}


def transform(lobbying_filings: pd.DataFrame, policy_queries: pd.DataFrame) -> pd.DataFrame:
    if lobbying_filings.empty or policy_queries.empty:
        return pd.DataFrame(columns=[
            "query_id", "title", "filing_count", "total_spend_usd",
            "top_clients_json", "top_filers_json",
        ])

    rows = []
    for _, q in policy_queries.iterrows():
        q_codes = _split_codes(q.get("issue_codes"))
        if not q_codes:
            continue
        mask = lobbying_filings["issue_codes"].apply(
            lambda s: bool(_split_codes(s) & q_codes)
        )
        matched = lobbying_filings[mask]
        if matched.empty:
            rows.append({
                "query_id": q["query_id"],
                "title": q["title"],
                "filing_count": 0,
                "total_spend_usd": 0.0,
                "top_clients_json": json.dumps([]),
                "top_filers_json": json.dumps([]),
            })
            continue

        spend = matched["income"].apply(_parse_money).sum()
        clients = Counter(matched["client_name"].dropna()).most_common(10)
        filers = Counter(matched["filer_org"].dropna()).most_common(10)

        # Per-client and per-filer spend
        client_spend: dict[str, float] = defaultdict(float)
        filer_spend: dict[str, float] = defaultdict(float)
        for _, r in matched.iterrows():
            amt = _parse_money(r.get("income"))
            if r.get("client_name"):
                client_spend[r["client_name"]] += amt
            if r.get("filer_org"):
                filer_spend[r["filer_org"]] += amt

        rows.append({
            "query_id": q["query_id"],
            "title": q["title"],
            "filing_count": int(len(matched)),
            "total_spend_usd": float(spend),
            "top_clients_json": json.dumps([
                {"client": c, "filing_count": n,
                 "total_spend": round(client_spend.get(c, 0.0), 2)}
                for c, n in clients
            ]),
            "top_filers_json": json.dumps([
                {"filer": f, "filing_count": n,
                 "total_spend": round(filer_spend.get(f, 0.0), 2)}
                for f, n in filers
            ]),
        })
    return pd.DataFrame(rows)
