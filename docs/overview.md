# Overview — what this is and why

## Mission
Serves **journalism and institutional accountability**: finding, verifying, and surfacing
true things about how power and money work. The standards *are* the product — a fabricated
number or unsourced claim defeats the purpose. Two rules recur in the code:
- **Never fabricate; fail loudly.** An unsourceable value is `null`/`unknown`; the pipeline
  halts or errors rather than inventing a number, URL, citation, or quote (a missing LLM
  backend raises; the runner rejects duplicate rows; the queue says "reviewing blind").
- **Expensive or irreversible steps sit behind human review.** `human_review_queue` halts
  the run; decisions are content-hashed so they survive re-runs.

## Vocabulary (locked 2026-07-04)
- **project** — the container directory holding everything below.
- **methodology** — the authored prose method (`methodology_raw.md`).
- **workflow** — the executable stage graph it compiles into (`compiled/*.json`, one
  validated `Stage` per file; a DAG of typed stages, every edge schema-validated).

A project dir also holds `code/`, `data/`, `runs/<id>/` (outputs + `manifest.json`),
and `decisions/` — runtime data, not source. Versions are documents in the store
(the `version` collection), not a project subdir.

## The three features
| Feature | Code | Status |
|---|---|---|
| **Runner** | `app/runtime/` | On master — executes a workflow (typed `Stage` end-to-end), validates I/O, persists, halts for review, resumes. |
| **Compiler** | `app/compiler/` | Engine on master (prose → LLM → validated workflow, re-ask on failure; `python -m app.compiler`); authoring UI in the PR stack. |
| **Eval** | `app/core/models/eval.py` | Data model only — `EvalConfig` + grain-preservation gate; no runner integration yet. |
