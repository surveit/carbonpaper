# prototype_one — reviewable AI methodology DAGs

A platform for running data/OSINT pipelines as **DAGs of typed nodes** instead of
opaque generic code. A "methodology" is a directed graph whose every edge is
schema-validated, whose expensive/irreversible steps sit behind human-review
gates, and whose runs are persisted with a full manifest — so an AI-driven
pipeline is *testable and reviewable*, not a black box.

## The core idea: a DAG artifact + three features on top of it

The unit of everything is a **DAG artifact**: a folder `examples/<name>/` with
`compiled/*.yaml` (one file per stage) and `methodology_raw.md` (the prose it was
compiled from). Runs land in `examples/<name>/runs/<run_id>/`.

Three independent features operate on that artifact:

| Feature | Code | What it does |
|---|---|---|
| **Runner** | `app/runtime/` | Executes a DAG: validates I/O between stages, persists outputs + `manifest.json`, halts for human review, resumes. |
| **Compiler** *(planned)* | — | Distills prose OR an unstructured Claude Code transcript into a *draft* DAG. |
| **Eval** *(planned)* | — | Checks a methodology reproduces ground truth (`examples/*/eval/`). |

**The contract — `app/models/`.** Pydantic models are the single source of truth
for the 7 node types (their executable handles, the column-type vocab, connector
kinds, aggregation formulas). Constructing a model validates it;
`validate_methodology(stages)` returns a non-fatal issue list and `parse_methodology`
raises. The compiler (planned) will emit *to* these models. (The runtime does not yet
parse stage dicts through them — wiring that in is the next step; see
`docs/models-and-storage.md`.)

## The 7 node types
`input_data` · `llm_transform` · `python_transform` · `join` · `aggregate` ·
`human_review_queue` · `publish`. Each stage YAML declares typed `inputs`, a typed
`output_schema`, and one executable-handle block (`connector`/`llm`/`function`/
`join`/`aggregate`/`queue`/`publish`). See `app/models/` for the contract.

## Running it
```
pip install -r requirements.txt          # fastapi, pandas, pyarrow, pyyaml, claude-agent-sdk, ...
python -m uvicorn app.main:app --port 8765   # web UI: DAG view, runs, review queue
python -m app.runtime.runner examples/<name> # run a methodology from the CLI
```
LLM stages run through the Claude Agent SDK (`claude_agent_sdk`), which drives the
installed `claude` CLI. Backend is selectable: `CW_LLM_BACKEND=agent_sdk|cli|mock`
(default `auto` → agent_sdk, else the CLI). It never silently falls back to the
mock; `CW_LLM_FORCE_MOCK=1` opts into the offline mock.

## Repo layout
```
app/models/           the node-type contract (Pydantic models)
app/SCHEMA.md         prose schema spec (legacy — superseded by app/models/)
app/runtime/          the Runner (executor, handlers, LLM backends, validation)  → app/runtime/AGENTS.md
app/main.py           thin FastAPI bootstrap; routes live in app/web/routers/   → app/AGENTS.md
app/web/              the web layer (routers, loading, diagrams, config)
app/services/         web-independent workflow logic (node review, versioning)
app/chat/             embeddable chat subsystem (PydanticAI; own backend env vars)
app/llm/              shared LLM vocabulary (the model menu)
app/templates/, app/static/   the web UI
tests/                pytest suite (offline: conftest forces the LLM mock)
examples/<name>/      DAG artifacts (compiled/ + methodology_raw.md + code/ + data/ + runs/)
```

## Docs (`docs/`)
- [docs/overview.md](docs/overview.md) — what this is and why; the examples.
- [docs/architecture.md](docs/architecture.md) — the code map.
- [docs/named-schemas.md](docs/named-schemas.md) — the named-schema data model + the eval model.
- [docs/run-and-review-ui.md](docs/run-and-review-ui.md) — run page, review queue, node review + versioning.
- [docs/RETHINK.md](docs/RETHINK.md) — the post-CongressWatch product critique (where this needs to go).
- [docs/models-and-storage.md](docs/models-and-storage.md) — storage convention + the plan to wire loaders through app/models.

## Conventions (load-bearing, not stylistic)
- **Never fabricate.** A value that can't be sourced is `null`/`unknown`; the
  pipeline fails loudly or halts rather than inventing a number, URL, or citation.
  The LLM backends are opt-in and never silently fall back to a mock.
- **`human_review_queue` is how we handle asymmetrical risk.** Where a wrong
  automated result is expensive or irreversible, gate that step behind human
  sign-off: the runner halts, and decisions are content-hashed so they survive
  re-runs.
