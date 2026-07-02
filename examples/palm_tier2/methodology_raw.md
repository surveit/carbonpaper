# palm_tier2 — distilled OSINT research pipeline

Distilled from 3 unstructured Claude Code research runs (see
`examples/palm_osint/research_runs/DISTILLATION.md`). The open-ended ~40-tool-call
exploration collapses to deterministic plumbing + 3 LLM judgment stages. Compiled
against `app/dag_schema.py`.

## §1 Seeds (input_data)
The mills to research: facility_id, UML id, name, operator PT, parent group,
province, coords.

## §2 Build queries (python_transform)
Deterministic: key on the UML id, prefer rspo.org PDFs, CDM for biogas, press for
PROPER — the query pattern every research run used.

## §3 Locate (llm_transform + WebSearch)  ← JUDGMENT #1
The "connector needs an LLM": search and pick the AUTHORITATIVE, MOST-RECENT docs.
Not a fixed URL. Returns one row per located doc.

## §4 Fetch (python_transform)
Deterministic urllib download to a cache — what the runs did in Bash. A 403/timeout
is recorded, not raised.

## §5 PDF→text (python_transform)
Deterministic local pypdf extraction — the runs' key technique (fetch tools fail on
the image PDFs).

## §6 Grep fields (python_transform)
Deterministic: window around fixed anchors (capacity, PalmGHG appendix, POME, OER,
certificate, coords) to narrow the doc before the LLM reads it.

## §7 Extract (llm_transform)  ← JUDGMENT #2
The one genuine extraction LLM: read the grepped snippets, emit fields with value,
confidence, source, primary/press grade. No tools.

## §8 Collate (python_transform)
Deterministic: group a facility's per-doc rows into one row for adjudication.

## §9 Adjudicate (llm_transform)  ← JUDGMENT #3
Reconcile conflicting figures across docs (100 vs 120 t/h), prefer recent primary,
drop unsupported. Could become a human_review_queue later.

## §10 Publish (publish)
Per-facility dossier of the reconciled, sourced fields.
