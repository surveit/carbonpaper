# Overview — what this is and why

## Product vision
AI is making data investigations cheaper to produce; deciding whether they hold up is
still hard. Carbon Paper is built for the person doing that review. It shows how the
result was produced and uses deterministic pipelines to rule out entire classes of
error, so human judgment can focus where it matters.

## Mission
Serves **journalism and institutional accountability**: finding, verifying, and surfacing
true things about how power and money work. The standards *are* the product — a fabricated
number or unsourced claim defeats the purpose. Two rules recur in the code:
- **Never fabricate; fail loudly.** An unsourceable value is `null`/`unknown`; the pipeline
  halts or errors rather than inventing a number, URL, citation, or quote (a missing LLM
  backend raises).
- **Expensive or irreversible steps sit behind human review.** `human_review_queue` halts
  the run; decisions are content-hashed so they survive re-runs.

## Vocabulary (locked 2026-07-04)
- **project** — the container directory holding everything below.
- **methodology** — the authored prose method (a `methodology` document).
- **workflow** — the executable stage graph it compiles into (the project's
  `working_copy` document; a DAG of typed stages whose schemas resolve from the graph).

A project dir also holds `code/`, `data/` and `runs/<id>/` (a run's parquet outputs,
its artifacts and its review queue) — runtime data, not source. Everything else a
project holds is a document in the store: its methodology and working copy, its
versions (`workflow_version`), each run's record and event log, and the review
decisions (`app.core.stage_cache`).

## The three features
| Feature | Code | Status |
|---|---|---|
| **Runner** | `app/runtime/` | On master — executes a workflow (typed `Stage` end-to-end), validates I/O, persists, halts for review, resumes. |
| **Compiler** | `app/compiler/` | On master — generates the data model and a stage's tests from the methodology document (LLM, re-ask on schema failure). Stages are authored by an MCP client through `app/services/stage_edit.py`, a batch at a time. |
| **Eval** | `app/models/eval.py` | Data model only — `EvalConfig` + grain-preservation gate; no runner integration yet. |
