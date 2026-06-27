# examples/palm_tier2 — the distilled OSINT research pipeline

Distilled from the 3 unstructured Claude Code research runs in
`examples/palm_osint/research_runs/` (see that folder's `DISTILLATION.md`). The
open-ended ~40-tool-call exploration collapses to **deterministic plumbing + only
3 LLM judgment stages**. Compiled against `app/dag_schema.py`.

## The flow — 10 stages, 3 of them LLM
```
seeds(input_data) → build_queries(py) → LOCATE(llm + WebSearch)
  → fetch_docs(py) → pdf_to_text(py) → grep_fields(py)
  → EXTRACT(llm) → collate(py) → ADJUDICATE(llm) → publish
```
The LLM sits at exactly the three irreducible judgment points the distillation
found: **locate** the authoritative/most-recent doc, **extract** fields from the
grepped snippets, **adjudicate** conflicts across a facility's docs. Everything
else — query construction, fetching, PDF→text, grepping, merging — is deterministic
Python. No new connector kind was needed: fetch/parse are `python_transform`s
(exactly what the research agents did with urllib + pypdf).

## Where the rich data is — `SOURCE_MAP.md`
4 deep-source agents mapped it: the rich operational data lives in the **full RSPO
ASA / recertification "Public Summary Reports"** (150–400pp, with the PalmGHG GHG
table + POME methane-capture split) and **CDM Project Design Documents** — not the
thin RSPO public *announcement*. `SOURCE_MAP.md` records the doc hierarchy, host
access routes (incl. the musimmas.com 504 workaround), URL patterns, and per-mill
recovered values. Those values double as the eval ground-truth.

## Eval ground-truth (wired, engine pending)
`eval/extract_truth.jsonl` — 29 sourced `(facility_id, field, expected_value,
source_url, grade)` rows from SOURCE_MAP. The extract stage carries a declarative
`eval:` block referencing it. The eval *engine* (compute coverage/agreement) is the
planned third platform feature — not built yet; this is the seed data for it.

## Stage internals worth knowing
- `fetch_docs.py` — browser User-Agent (bare bot UAs get 403'd by corporate WAFs
  even on public PDFs), **retry-on-504** (Musim Mas static PDFs 504 transiently),
  and a **Wayback fallback**. Failures are recorded as data, not raised.
- `pdf_to_text.py` — cap raised 60K→2M chars: the PalmGHG appendix is at the END of
  a 150–400pp report; the old cap truncated before it (the single biggest cause of
  under-collection).
- `grep_fields.py` — widened anchors (PalmGHG, POME methane split, OER/KER, cert
  no.+dates, peat area, production, biogas MW), multiple windows per anchor.
- `adjudicate` — resolves capacity discrepancies (flag, never average), distinguishes
  CDM "designed vs operated/CERs-issued", rejects name collisions.

## Run it
```
python -m app.runtime.runner examples/palm_tier2          # agent_sdk — real web research, SLOW
CW_LLM_FORCE_MOCK=1 python -m app.runtime.runner ...      # plumbing only (LLM stages stubbed)
```
**Known issue:** `locate` is heavy (~3–4 min/mill; it hunts the audit series across
multiple hosts), so a 5-mill run is ~15 min and the live field-count
re-verification of the hardened pipeline is still pending. Tightening the `locate`
prompt (find 2–3 best URLs fast, don't deep-research) is the next step. `runs/`,
`decisions/`, and the `build/` fetch cache are gitignored.
