"""
Deterministic: locate the relevant passages in a doc before handing them to the
EXTRACT llm — so the model reads a few KB of on-point text, not the whole 150–400
page report. The distillation's "fixed grep keys", now widened to the full RSPO
Public Summary / CDM PDD field set (SOURCE_MAP.md): capacity, the PalmGHG /
Summary-of-Net-GHG appendix, the POME methane-capture SPLIT (electricity vs
flaring %), OER/KER, certificate number + effective/expiry dates, planted &
peat area, CPO/PK/FFB production, certification body, biogas MW, supply-chain
model. The PalmGHG appendix is at the END of the doc and may recur per audit
year, so we collect MULTIPLE non-overlapping windows per anchor (not just the
first hit) with generous windows. NO LLM.
"""

from __future__ import annotations

import json
import re

import pandas as pd

# anchor regex -> field key. Case-insensitive. Each anchor may match many times
# across a multi-audit report; we keep several windows per key (see _MAX_HITS).
_ANCHORS = {
    # nameplate / processing capacity (t FFB per hour)
    "capacity": r"(?:capacity|kapasitas|nameplate|throughput)[^.\n]{0,60}"
                r"(?:MT|ton(?:ne|nes)?|t)\s*/?\s*(?:hr|hour|h|jam)",
    # the PalmGHG / Net-GHG appendix — the prize. Match the table headers/units.
    "palmghg": r"(PalmGHG|Summary of Net GHG|net GHG|GHG emission[s]?|"
               r"kg\s?CO2\s?eq|tCO2e|t\s?CO2e|CO2e\s*/\s*t\s*CPO|kg CO2/MT)",
    # POME / effluent treatment description
    "pome": r"(POME|palm oil mill effluent|effluent|anaerobic|aerobic|"
            r"land application|IPAL|ponding|lagoon|BOD|COD)",
    # methane capture + the electricity-vs-flaring SPLIT
    "methane_split": r"(methane captur\w*|biogas|methane (?:to|for) (?:electricity|power|grid|engine)|"
                     r"flar\w+|divert\w* to methane|gas engine|\d{1,3}\s?%[^.\n]{0,40}"
                     r"(?:electricity|flar|methane))",
    # oil / kernel extraction rates
    "oer_ker": r"\b(OER|KER|oil extraction rate|kernel extraction rate|"
               r"extraction rate)\b[^.\n]{0,40}\d",
    # certificate number
    "certificate": r"(Certificate (?:No|Number)|RSPO[- ]?\d{4,}|"
                   r"(?:CU|MUTU|SUCO|SGS|BSI)[- ]?RSPO[- ]?\w+|cert(?:ificate)? id)",
    # certificate validity window — effective / issue / expiry dates
    "cert_dates": r"(valid(?:ity)?|effective|date of issue|issued on|expir\w+|"
                  r"date of expiry|certification period|valid (?:from|until|through))",
    # certification body / accredited CB
    "cert_body": r"(certification body|certificate holder|accredited by|"
                 r"Sucofindo|SICS|Control Union|Mutuagung|MUTU|SGS|BSI|TUV|"
                 r"PT\s+\w+\s+Certification)",
    # planted area / total planted hectares
    "planted_area": r"(planted area|total planted|area planted|hektar|hectares?|"
                    r"\bha\b)[^.\n]{0,40}\d",
    # area on peat
    "peat_area": r"(peat|gambut|histosol)[^.\n]{0,60}(?:hectares?|\bha\b|\d)",
    # CPO / PK / FFB production tonnages
    "production": r"(CPO|crude palm oil|PK\b|palm kernel|FFB|fresh fruit bunch(?:es)?|"
                  r"production|processed|tonn?e)[^.\n]{0,40}\d{2,}",
    # supply base / supplying estates / smallholders
    "supply_base": r"(supply base|supplying estate|own estate|scheme smallholder|"
                   r"independent smallholder|outgrower|plasma|FFB suppl)",
    # CDM PDD specifics: biogas MW, generator/engine count, CER schedule
    "biogas_cdm": r"(\d[\d.,]*\s?(?:MW|kW)|biogas (?:plant|power)|gas engine|"
                  r"CER[s]?\b|emission reduction[s]?|tCO2e\s*/\s*(?:yr|year|annum)|"
                  r"PDD|Project Design Document)",
    # supply-chain / certification model
    "supply_chain_model": r"(Identity Preserved|Mass Balance|Segregat\w+|Book and Claim|"
                          r"\bIP\b|\bMB\b|\bSG\b|supply chain (?:model|certification))",
    "coordinates": r"(\d{1,3}[°º]\s*\d|latitude|longitude|GPS)",
}
_WINDOW = 400      # chars each side of an anchor hit (generous; grep windows, LLM reads)
_MAX_HITS = 3      # keep up to this many non-overlapping windows per anchor
_MAX_KEY_CHARS = 4000  # cap total snippet text per key (across its windows)


def _snippets(text: str) -> dict:
    if not text:
        return {}
    out: dict[str, str] = {}
    for key, pat in _ANCHORS.items():
        windows: list[str] = []
        last_end = -1
        try:
            matches = re.finditer(pat, text, re.I)
        except re.error:
            matches = iter(())
        for m in matches:
            # skip a hit that falls inside the window we just captured
            if m.start() <= last_end:
                continue
            s = max(0, m.start() - _WINDOW)
            e = min(len(text), m.end() + _WINDOW)
            windows.append(re.sub(r"\s+", " ", text[s:e]).strip())
            last_end = e
            if len(windows) >= _MAX_HITS:
                break
        if windows:
            out[key] = (" … ".join(windows))[:_MAX_KEY_CHARS]
    return out


def transform(parsed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in parsed.iterrows():
        snips = _snippets(r.get("doc_text") or "")
        rows.append({
            "facility_id": r.get("facility_id"),
            "name": r.get("name"),
            "doc_type": r.get("doc_type"),
            "url": r.get("url"),
            "parse_status": r.get("parse_status"),
            "n_anchors": len(snips),
            "field_snippets_json": json.dumps(snips, ensure_ascii=False),
        })
    return pd.DataFrame(rows)
