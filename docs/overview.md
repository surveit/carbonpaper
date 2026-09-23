# Overview — what this is and why

## Product vision
A chat is a good place to investigate and a terrible place to review. Carbon Paper
turns open-ended work into a constrained workflow where deterministic steps are
predictable and judgment calls are visible. The same structure that makes review
faster makes the investigation easy to rerun and reuse.

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
- **workflow** — the executable stage graph it compiles into (the project's newest
  `workflow_version`; a DAG of typed stages whose schemas resolve from the graph).

A project dir also holds `runs/<id>/` (a run's parquet outputs,
its artifacts and its review queue) — runtime data, not source. Everything else a
project holds is a document in the store: its methodology, its drafts, its
versions (`workflow_version`), each run's record and event log, and the review
decisions (`app.models.records.review_decision`).

## The three features
| Feature | Code | Status |
|---|---|---|
| **Runner** | `app/runtime/` | On master — executes a workflow (typed `Stage` end-to-end), validates I/O, persists, halts for review, resumes. |
| **Compiler** | `app/compiler/` | On master — generates a version's review guide from its stages and the methodology document, and a stage's tests from a finished run's real rows (LLM, re-ask on schema failure). Stages are authored by an MCP client through `app/services/stage_edit.py`, a batch at a time. |
| **Eval** | `app/evals/`, `app/models/eval.py` | On master — runs an `EvalConfig` against a pinned workflow version and scores it declaratively; a path that is not grain-preserving is recorded as `vetoed`. |
