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
| **Compiler** | `app/compiler.py` | Distills prose OR an unstructured Claude Code transcript into a *draft* DAG. |
| **Eval** *(planned)* | — | Checks a methodology reproduces ground truth (`examples/*/eval/`). |

**The clean interface — `app/dag_schema.py`.** This is the single coupling point:
the canonical, machine-readable contract for the 7 node types (their executable
handles, the column-type vocab, connector kinds, aggregation formulas) plus
`validate_stage` / `validate_dag` / `validate_methodology`. The runtime validates
*against* it; the compiler emits *to* it; **neither imports the other**. Keep it
pure (no runtime/compiler imports) so it stays a trustworthy interface. The prose
companion is `app/SCHEMA.md`.

## The 7 node types
`input_data` · `llm_transform` · `python_transform` · `join` · `aggregate` ·
`human_review_queue` · `publish`. Each stage YAML declares typed `inputs`, a typed
`output_schema`, and one executable-handle block (`connector`/`llm`/`function`/
`join`/`aggregate`/`queue`/`publish`). See `app/dag_schema.py` for the contract.

## Running it
```
pip install -r requirements.txt          # fastapi, pandas, pyarrow, pyyaml, claude-agent-sdk, ...
python -m uvicorn app.main:app --port 8765   # web UI: DAG view, runs, review queue, /compile
python -m app.runtime.runner examples/<name> # run a methodology from the CLI
```
LLM stages run through the Claude Agent SDK (`claude_agent_sdk`), which drives the
installed `claude` CLI. Backend is selectable: `CW_LLM_BACKEND=agent_sdk|cli|mock`
(default `auto`), or `CW_LLM_FORCE_MOCK=1` for a deterministic offline run.

## Repo layout
```
app/dag_schema.py     the node-type contract (the interface)
app/SCHEMA.md         prose schema spec
app/runtime/          the Runner (executor, handlers, LLM backends, validation)  → app/runtime/AGENTS.md
app/compiler.py       the Compiler (transcript/prose → draft DAG)
app/main.py           FastAPI web app (DAG/run/queue/compile pages)              → app/AGENTS.md
app/templates/, app/static/   the web UI
examples/<name>/      DAG artifacts (compiled/ + methodology_raw.md + code/ + data/ + runs/)  → examples/*/AGENTS.md
```

## Conventions (load-bearing, not stylistic)
- **Never fabricate.** A value that can't be sourced is `null`/`unknown`; the
  pipeline fails loudly or halts rather than inventing a number, URL, or citation.
- **Every value carries provenance** — its source URL/publisher travels with it.
- **Gate the expensive/irreversible step** behind `human_review_queue` (halts the
  run; decisions are content-hashed so they survive re-runs).
- **Adversarially verify LLM output** before it becomes asset; demote/​drop the
  unverified.
