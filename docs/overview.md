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

## Vocabulary (locked 2026-07-04; source to connector added 2026-09-28)
- **project** — the container directory holding everything below.
- **methodology** — the authored prose method (a `methodology` document).
- **workflow** — the executable stage graph it compiles into (the project's newest
  `workflow_version`; a DAG of typed stages whose schemas resolve from the graph).
- **source** — a stored file (`ProjectFile`, `app/core/files.py`) as a run reads it, named
  by `source_id` and `source_sha256`. A fetched one carries `origin_url` and `fetched_at`,
  which only `receive_source` and `receive_mirrored_source` write; an upload carries
  neither. The Files pages keep the word "file".
- **span** — a verbatim `quote` and where it sits in one source (`Span`,
  `app/models/spans.py`); `span` and `list[span]` are column types.
- **locator** — a span's address in its source: one `Locator` subclass per kind
  (`page_char_range`, `char_range`, `cell`; `app/models/locators.py`). Only a
  `page_char_range` span is read back and verified.
- **judgment** — a decision code did not compute. A model's is a `Judgment`
  (`app/core/judgments.py`: system prompt, task, model, reply, usage), which the row's cache entry
  and run-log event name by `judgment_id`; a reviewer's stays a review decision
  (`ReviewDecision`).
- **method** — what a saved version kept of its project's row types, verbs and methodology
  (`Method`, on `WorkflowVersion.method`). "methodology" stays the prose alone.
- **pack** — a subpackage `app/packs/<pack_id>/` that registers connector kinds when
  `app.packs` is imported ([packs.md](packs.md)). Everything else under `app/` is the kernel.
- **connector** — how an `input_data` stage reaches its files (`Connector`): kind `file`
  reads the paths it is given; a pack's kind (`ConnectorSpec`, e.g. `recap_docket`)
  acquires bytes the kernel stores as sources.

A project dir also holds `runs/<id>/` (a run's parquet outputs, its source tables,
its artifacts and its review queue) — runtime data, not authored. Everything else a
project holds is a document in the store: its methodology, its drafts, its
versions (`workflow_version`), each run's record and event log, the review
decisions (`app.models.records.review_decision`) and the model judgments.

## The three features
| Feature | Code | Status |
|---|---|---|
| **Runner** | `app/runtime/` | On master — executes a workflow (typed `Stage` end-to-end), validates I/O, persists, halts for review, resumes. |
| **Compiler** | `app/compiler/` | On master — generates a version's review guide from its stages and the methodology document, and a stage's tests from a finished run's real rows (LLM, re-ask on schema failure). Stages are authored by an MCP client through `app/services/stage_edit.py`, a batch at a time. |
| **Eval** | `app/evals/`, `app/models/eval.py` | On master — runs an `EvalConfig` against a pinned workflow version and scores it declaratively; a path that is not grain-preserving is recorded as `vetoed`. |
