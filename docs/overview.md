# Overview — what this is and why

## Mission
Serves **journalism and institutional accountability**: finding, verifying, and surfacing
true things about how power and money work. The standards *are* the product — a fabricated
number or unsourced claim defeats the purpose. Two rules recur in the code:
- **Never fabricate; fail loudly.** An unsourceable value is `null`/`unknown`; the pipeline
  halts or errors rather than inventing a number, URL, citation, or quote (a missing LLM
  backend raises; the runner rejects duplicate rows; the review queue states that no
  primary key is declared rather than guessing which columns identify a row).
- **Expensive or irreversible steps sit behind human review.** `human_review_queue` halts
  the run; decisions are content-hashed so they survive re-runs.

## Vocabulary (locked 2026-07-04)
- **project** — the container directory holding everything below.
- **methodology** — the authored prose method (`methodology_raw.md`).
- **workflow** — the executable stage graph it compiles into (`compiled/*.json`, one
  validated `Stage` per file; a DAG of typed stages, every edge schema-validated).

A project dir also holds `code/`, `data/`, `runs/<id>/` (outputs + `manifest.json`) —
runtime data, not source. Review decisions are documents in the store
(`app.core.stage_cache`), not a project subdir. Versions are documents in the store
(the `workflow_version` collection), not a project subdir.

## The three features
| Feature | Code | Status |
|---|---|---|
| **Runner** | `app/runtime/` | On master — executes a workflow (typed `Stage` end-to-end), validates I/O, persists, halts for review, resumes. |
| **Compiler** | `app/compiler/` | On master — generates the data model and a stage's tests from the methodology document (LLM, re-ask on schema failure). Stages are authored by an MCP client through `app/services/stage_edit.py`, a batch at a time. |
| **Eval** | `app/models/eval.py` | Data model only — `EvalConfig` + grain-preservation gate; no runner integration yet. |
