"""
Substrate decision (osint_integration_thinking.md §2 / Devil's-Advocate #3):
flatten the nested `FacilityRecord` document into typed, flat columns so the
prototype_one runtime — which passes flat pandas DataFrames between stages —
can carry it AND so `validation.py` can actually SEE the provenance fields
(capacity value, units, source URL, provenance class), rather than treating a
nested `json` blob as opaque.

The lossy part is named honestly in the YAML compiler_notes: variable-length
lists (`aliases`, `contributing_sources`, `reported_emissions`) are preserved
as JSON-encoded columns. The scalar provenance that matters most for an
auditable asset — every Quantity's value/unit/source — is promoted to first
class columns (`capacity_value`, `capacity_unit`, `capacity_source_url`,
`capacity_provenance`).

Input:  the raw rows of data/facilities.jsonl (nested dict/list cells).
Output: one flat row per facility.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    # nested-null cells come back as NaN (float) from pandas read_json
    if isinstance(v, float):
        try:
            return pd.isna(v)
        except (TypeError, ValueError):
            return False
    return False


def _get(obj: Any, *path: str) -> Any:
    """Walk a nested dict by key path; return None if any hop is missing."""
    cur = obj
    for key in path:
        if _is_missing(cur) or not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return None if _is_missing(cur) else cur


def _jsonify(v: Any) -> str:
    """Serialize a list/dict cell to a compact JSON string. Lists from pandas
    may be numpy arrays — coerce to plain lists first."""
    if _is_missing(v):
        return "[]"
    if isinstance(v, str):
        return v
    try:
        if hasattr(v, "tolist"):
            v = v.tolist()
        return json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(v))


def _uml_from_facility_id(fid: str) -> str:
    """facility_id is `palm:<UML_ID>` (or `palm:<geocell-key>` for keyless
    records). Strip the `palm:` prefix to recover the join key against Trase."""
    s = str(fid)
    return s.split(":", 1)[1] if ":" in s else s


def transform(facilities: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, rec in facilities.iterrows():
        loc = rec.get("location")
        cap = rec.get("nameplate_capacity")
        contributing = rec.get("contributing_sources")
        n_sources = 0
        if not _is_missing(contributing):
            try:
                n_sources = len(contributing)
            except TypeError:
                n_sources = 0

        rows.append({
            "facility_id": rec.get("facility_id"),
            "uml_id": _uml_from_facility_id(rec.get("facility_id")),
            "name": rec.get("name"),
            "owner": None if _is_missing(rec.get("owner")) else rec.get("owner"),
            "parent_group": None if _is_missing(rec.get("parent_group")) else rec.get("parent_group"),
            "country": _get(loc, "country"),
            "region": _get(loc, "region"),
            "lat": _get(loc, "lat"),
            "lon": _get(loc, "lon"),
            # Promoted Quantity provenance — the fields validation must see.
            "capacity_value": _get(cap, "value"),
            "capacity_unit": _get(cap, "unit"),
            "capacity_provenance": _get(cap, "provenance"),
            "capacity_source_url": _get(cap, "source", "url"),
            "owner_source_url": _get(rec.get("owner_source"), "url"),
            "has_capacity": int(_get(cap, "value") is not None),
            "n_sources": int(n_sources),
            "multi_source": bool(n_sources >= 2),
            "has_enrichment": bool(rec.get("has_enrichment")),
            "dedup_key": rec.get("dedup_key"),
            "first_seen": None if _is_missing(rec.get("first_seen")) else rec.get("first_seen"),
            "last_refreshed": None if _is_missing(rec.get("last_refreshed")) else rec.get("last_refreshed"),
            # Lossy-but-preserved: keep the raw provenance lists as JSON so the
            # asset isn't silently dropped (DA#3). validation treats these as
            # opaque — documented in compiler_notes.
            "aliases_json": _jsonify(rec.get("aliases")),
            "contributing_sources_json": _jsonify(contributing),
            "reported_emissions_json": _jsonify(rec.get("reported_emissions")),
        })
    return pd.DataFrame(rows)
