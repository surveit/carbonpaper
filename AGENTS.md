# prototype_one — reviewable AI workflows

A platform for running data/OSINT pipelines as **workflows of typed stages**
instead of opaque generic code: every edge between stages is schema-validated,
expensive/irreversible steps sit behind human-review gates, and every run is
persisted with a full manifest — so an AI-driven pipeline is *testable and
reviewable*, not a black box.

Vocabulary: a **project** is the container directory; a **methodology** is the
authored prose method; a **workflow** is the executable stage graph the
methodology compiles into.

This file is an **index**, not a code map — it points at where things live and
where to read next. It deliberately does not describe the architecture itself;
that description has exactly one home (see below), so it can't drift.

## Start here
- **Code map (canonical):** [docs/architecture.md](docs/architecture.md) —
  what lives where, and the placement rules for where new code should go.
- **What this is and why:** [docs/overview.md](docs/overview.md) — mission,
  vocabulary, feature status.

## Subsystems (each documents itself)
- `app/` — the FastAPI web app (routes, templates, the node/stage panel) →
  [app/AGENTS.md](app/AGENTS.md)
- `app/runtime/` — the Runner (executor, stage handlers, LLM backends,
  validation) → [app/runtime/AGENTS.md](app/runtime/AGENTS.md)

## Docs (`docs/`) — the system as it exists today
- [docs/architecture.md](docs/architecture.md) — the code map (canonical).
- [docs/overview.md](docs/overview.md) — what this is and why; the vocabulary;
  feature status.
- [docs/named-schemas.md](docs/named-schemas.md) — the named-schema data model
  + the eval model.
- [docs/run-and-review-ui.md](docs/run-and-review-ui.md) — run page, review
  queue, node review + versioning.
- [docs/models-and-storage.md](docs/models-and-storage.md) — the storage
  convention.

## `/plans` — scratch cache, not evidence
`/plans` is a scratch cache of in-flight planning and design thinking (e.g.
[plans/naming-refactor.md](plans/naming-refactor.md),
[plans/RETHINK.md](plans/RETHINK.md)). It is **not** evidence — never cite it
for how the code works. To learn what the code does, read the code or
`/docs`. Plans go stale; prune them freely once they're superseded or merged.

## Running it
```
pip install -r requirements.txt          # fastapi, pandas, pyarrow, claude-agent-sdk, ...
python -m uvicorn app.main:app --port 8765   # web UI: workflow view, runs, review queue
python -m app.runtime.runner examples/<name> # run a project's workflow from the CLI
```
LLM stages run through the Claude Agent SDK (`claude_agent_sdk`), which drives the
installed `claude` CLI. Backend is selectable: `CW_LLM_BACKEND=agent_sdk|cli|mock`
(default `auto` → agent_sdk, else the CLI). It never silently falls back to the
mock; `CW_LLM_FORCE_MOCK=1` opts into the offline mock.

## Conventions (load-bearing, not stylistic)
- **Never fabricate.** A value that can't be sourced is `null`/`unknown`; the
  pipeline fails loudly or halts rather than inventing a number, URL, or citation.
  The LLM backends are opt-in and never silently fall back to a mock.
- **Schemas are called schemas.** A stage's `output_schema`, an input `schema:`
  block, a `TableSchema` — these are *schemas*, and that is the word, in code,
  comments, docs, and PR prose. Don't dress them up as "contracts" ("stage
  contract", "producer contract", "response contract"): the word adds no meaning
  and splits one concept across two names.
- **`human_review_queue` is how we handle asymmetrical risk.** Where a wrong
  automated result is expensive or irreversible, gate that step behind human
  sign-off: the runner halts, and decisions are content-hashed so they survive
  re-runs.
