"""
Deterministic: turn each fetched doc into plain text. The distillation's hardest-
won lesson — fetch tools fail on RSPO/CDM image PDFs, so extract locally. Tries
pypdf for PDFs, falls back to a best-effort decode for HTML/text. NO LLM.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Cap on the FULL parsed text we carry downstream to grep_fields.
#
# The old 60K cap was the single biggest cause of under-collection: the RSPO
# Public Summary Reports run 150–400 pages and the PalmGHG "Summary of Net GHG"
# appendix (the prize: kg CO2e/t CPO + the POME methane-capture split %) sits at
# the END of the document. 60K chars is ~25–30 pages, so we were truncating the
# report long before the appendix — grep then never saw the anchor.
#
# grep_fields still windows this down to ~a few KB of on-point snippets before the
# EXTRACT llm reads it, so keeping the full text here does NOT bloat the LLM call —
# it only lets the anchors be findable anywhere in the doc. At ~2–3K chars/page a
# 400-page report is ~1M chars; 2M is generous headroom while still bounding memory
# against a pathological file.
_MAX_CHARS = 2_000_000  # was 60_000 — see note above; the GHG appendix is at the END


def _to_text(path: str | None) -> dict:
    # A failed/absent fetch leaves local_path as None — but once the column has
    # gone through a pandas Series (row.to_dict()), that None surfaces as a float
    # NaN, which is truthy. Guard on pd.isna so `Path(nan)` never gets called.
    if path is None or (not isinstance(path, str) and pd.isna(path)) or not path or not Path(path).exists():
        return {"doc_text": "", "n_chars": 0, "parse_status": "no_file"}
    p = Path(path)
    try:
        if p.suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except Exception:
                return {"doc_text": "", "n_chars": 0, "parse_status": "pypdf_missing"}
            reader = PdfReader(str(p))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
            status = f"pdf:{len(reader.pages)}p"
        else:
            raw = p.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            # crude tag strip for HTML
            import re
            text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            status = "html"
        text = text[:_MAX_CHARS]
        return {"doc_text": text, "n_chars": len(text), "parse_status": status}
    except Exception as exc:  # noqa: BLE001
        return {"doc_text": "", "n_chars": 0, "parse_status": f"error:{type(exc).__name__}"}


def transform(fetched: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in fetched.iterrows():
        rows.append({**r.to_dict(), **_to_text(r.get("local_path"))})
    return pd.DataFrame(rows)
