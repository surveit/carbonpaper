# prototype_one — reviewable AI methodology DAGs

A platform for running data/OSINT pipelines as **DAGs of typed nodes** instead of
opaque generic code. A "methodology" is a directed graph whose every edge is
schema-validated, whose expensive/irreversible steps sit behind human-review
gates, and whose runs are persisted with a full manifest — so an AI-driven
pipeline is *testable and reviewable*, not a black box.

## The core idea: a DAG artifact + three features on top of it

The unit of everything is a **DAG artifact**: a folder `examples/<name>/` with
`compiled/<NN>_<stage_id>.json` (one file per stage, the JSON dump of the
validated `Stage` model; the `NN_` prefix orders the stage list in the UI) and
`methodology_raw.md` (the prose it was compiled from). Runs land in
`examples/<name>/runs/<run_id>/`.

Three independent features operate on that artifact:

| Feature | Code | What it does |
|---|---|---|
| **Runner** | `app/runtime/` | Executes a DAG: validates I/O between stages, persists outputs + `manifest.json`, halts for human review, resumes. |
| **Compiler** *(planned)* | — | Distills prose OR an unstructured Claude Code transcript into a *draft* DAG. |
| **Eval** *(planned)* | — | Checks a methodology reproduces ground truth (`examples/*/eval/`). |

**The contract — `app/models/`.** Pydantic models are the single source of truth
for the 8 node types (their executable handles, the column-type vocab, connector
kinds, aggregation formulas). Constructing a model validates it;
`validate_methodology(stages)` returns a non-fatal issue list and `parse_methodology`
raises. `app/models/loader.py` enforces the contract at load: the runner refuses to
execute a DAG with an invalid stage (`MethodologyLoadError`), and the viewer renders
per-file issues instead of crashing. The compiler (planned) will emit *to* these
models. See `docs/models-and-storage.md`.

## The 8 node types
`input_data` · `llm_transform` · `python_row_function` · `python_frame_function` ·
`join` · `aggregate` · `human_review_queue` · `publish`. Prefer `python_row_function`
(runtime-enforced 1:1) over `python_frame_function` unless the logic needs the whole frame. Each compiled stage JSON declares typed `inputs`, a typed
`output_schema`, and one executable-handle block (`connector`/`llm`/`function`/
`join`/`aggregate`/`queue`/`publish`). See `app/models/` for the contract.

## Running it
```
pip install -r requirements.txt          # fastapi, pandas, pyarrow, claude-agent-sdk, ...
python -m uvicorn app.main:app --port 8765   # web UI: DAG view, runs, review queue
python -m app.runtime.runner examples/<name> # run a methodology from the CLI
```
LLM stages run through the Claude Agent SDK (`claude_agent_sdk`), which drives the
installed `claude` CLI. Backend is selectable: `CW_LLM_BACKEND=agent_sdk|cli|mock`
(default `auto` → agent_sdk, else the CLI). It never silently falls back to the
mock; `CW_LLM_FORCE_MOCK=1` opts into the offline mock.

## Module organization: subsystems over a shared core

`app/` is organized by **what the software does**, not by framework layer. A
top-level package under `app/` is exactly one of two kinds:

- A **subsystem** — a feature with its own lifecycle that owns its internals,
  *including its own routes and templates if it has a UI* (`web`, `compiler`,
  `runtime`, `chat`). Subsystems do not import each other.
- **Shared core** — code that two or more subsystems depend on (`models`,
  `services`, `llm`). The core imports nothing back from any subsystem.

Dependencies point one way, always toward the core:

```
subsystems:   web    compiler    runtime    chat
                \        \          |        /
                 ▼        ▼         ▼       ▼
shared core:      services      llm      models
                              (models imports nothing from app at all)
```

**Where does a new module go?** Answer the first question that fits:

- Only declares/validates data shapes, opens no files, imports no other `app`
  package → `app/models/` (core). Typed shapes live here; the code that *builds*
  them from disk does not.
- Reads or writes the on-disk project store under `examples/<name>/` (loading,
  compilation records, node review, versioning, project status) → `app/services/`
  (core).
- Low-level LLM / `claude` CLI plumbing shared by more than one subsystem →
  `app/llm/` (core).
- One feature's own engine or its UI → that subsystem's package.

A generic name like "services" is a standing invitation to dump unrelated code
there — before adding to it, confirm the module really operates on the project
store; if it's plumbing for one feature, it belongs in that feature's package.

## Repo layout
```
── shared core ──
app/models/           typed node/stage models (Pydantic); imports nothing from app
app/services/         the on-disk project store: loader, compilation, node_review, versioning
app/llm/              shared LLM / claude-CLI vocabulary (the model menu; + llm_sdk, see below)
── subsystems ──
app/runtime/          the Runner (executor, handlers, LLM backends, validation)  → app/runtime/AGENTS.md
app/compiler/         prose → LLM → DAG authoring engine (python -m app.compiler)
app/web/              the web layer (routers, loading, diagrams, config)
app/chat/             embeddable chat subsystem (PydanticAI; own backend env vars)
app/main.py           thin FastAPI bootstrap; mounts app/web/routers/ + app/chat  → app/AGENTS.md
app/templates/, app/static/   web UI assets
── other ──
app/SCHEMA.md         prose schema spec (legacy — superseded by app/models/)
tests/                pytest suite (offline: conftest forces the LLM mock)
examples/<name>/      DAG artifacts (compiled/ + methodology_raw.md + code/ + data/ + runs/)
```

**Known deviations from the rule (to converge, not yet done):**
- `app/templates/` and `app/static/` sit at the `app/` root but are the `web`
  subsystem's assets; they belong under `app/web/` (as `app/chat/` already nests
  its own templates). Deferred because moving them rewrites every template path.
- LLM plumbing is split between `app/llm/options.py` and the loose module
  `app/llm_sdk.py`; the latter should fold into `app/llm/` (e.g. `app/llm/sdk.py`).

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
