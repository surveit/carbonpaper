# prototype_one — reviewable AI workflows

A platform for running data/OSINT pipelines as **workflows of typed stages**
instead of opaque generic code. A workflow is a directed graph whose every edge
is schema-validated, whose expensive/irreversible steps sit behind human-review
gates, and whose runs are persisted with a full manifest — so an AI-driven
pipeline is *testable and reviewable*, not a black box.

Vocabulary (locked; see `docs/naming-refactor.md`): a **project** is the
container directory; a **methodology** is the authored prose method; a
**workflow** is the executable stage graph the methodology compiles into.

## The core idea: a project artifact + three features on top of it

The unit of everything is a **project**: a folder `examples/<name>/` with
`compiled/<NN>_<stage_id>.json` (one file per stage, the JSON dump of the
validated `Stage` model; the `NN_` prefix orders the stage list in the UI) and
`methodology_raw.md` (the prose the workflow was compiled from). Runs land in
`examples/<name>/runs/<run_id>/`. Project directories are runtime data, not
source — `examples/` is untracked.

Three independent features operate on that artifact:

| Feature | Code | What it does |
|---|---|---|
| **Runner** | `app/runtime/` | Executes a workflow: validates I/O between stages, persists outputs + `manifest.json`, halts for human review, resumes. |
| **Compiler** | `app/compiler/` | Distills prose into a *draft* workflow (LLM call + validate + re-ask on failure). Engine on master; the authoring UI is in the open PR stack. |
| **Eval** *(model only)* | `app/models/eval.py` | Checks a workflow reproduces ground truth. `EvalConfig`/`EvalRun` exist as validated models; no runner integration yet. |

**The schemas — `app/models/`.** Pydantic models are the single source of
truth for the 8 stage types (their executable handles, the column-type vocab,
connector kinds, aggregation formulas). Constructing a model validates it;
`validate_workflow(stages)` returns a non-fatal issue list and `parse_workflow`
raises. `app/services/loader.py` enforces this at load: the runner refuses to
execute a workflow with an invalid stage (`WorkflowLoadError`), and the viewer
renders per-file issues instead of crashing. The compiler emits *to* these
models. See `docs/models-and-storage.md`.

## The 8 stage types
`input_data` · `llm_transform` · `python_row_function` · `python_frame_function` ·
`join` · `aggregate` · `human_review_queue` · `publish`. Prefer `python_row_function`
(runtime-enforced 1:1) over `python_frame_function` unless the logic needs the whole frame. Each compiled stage specification declares typed `inputs`, a typed
`output_schema`, and one executable-handle block (`connector`/`llm`/`function`/
`join`/`aggregate`/`queue`/`publish`). See `app/models/` for the schemas.

## Running it
```
pip install -r requirements.txt          # fastapi, pandas, pyarrow, claude-agent-sdk, ...
python -m uvicorn app.main:app --port 8765   # web UI: workflow view, runs, review queue
python -m app.runtime.runner examples/<name> # run a project's workflow from the CLI
```
LLM stages (`llm_transform`) call the model directly via `app.runtime.agent`, one
conversation per row. Backend is selectable: `CW_LLM_BACKEND=auto|cli|agent_sdk|anthropic`
(default `auto` → the claude CLI bridge; `anthropic` needs `ANTHROPIC_API_KEY`). A
requested backend that isn't available raises rather than falling back.

## Repo layout
```
app/models/           the stage-type schemas (Pydantic models) — the schema spec
app/runtime/          the Runner (executor, stages/, LLM backends, validation)  → app/runtime/AGENTS.md
app/compiler/         prose → LLM → workflow authoring engine (python -m app.compiler)
app/main.py           thin FastAPI bootstrap; routes live in app/web/routers/   → app/AGENTS.md
app/web/              the web layer (routers, loading, diagrams, config)
app/services/         web-independent workflow logic (loader, compilation, node review, versioning)
app/chat/             embeddable chat subsystem (PydanticAI; own backend env vars)
app/llm/              shared LLM vocabulary (the model menu)
app/templates/, app/static/   the web UI
tests/                pytest suite (offline: conftest forces the LLM mock)
examples/<name>/      project dirs (untracked runtime data: compiled/ + methodology_raw.md + code/ + data/ + runs/)
```

## Docs (`docs/`)
- [docs/overview.md](docs/overview.md) — what this is and why; the vocabulary; feature status.
- [docs/architecture.md](docs/architecture.md) — the code map.
- [docs/named-schemas.md](docs/named-schemas.md) — the named-schema data model + the eval model.
- [docs/run-and-review-ui.md](docs/run-and-review-ui.md) — run page, review queue, node review + versioning.
- [docs/RETHINK.md](docs/RETHINK.md) — the post-CongressWatch product critique (where this needs to go).
- [docs/naming-refactor.md](docs/naming-refactor.md) — the project/methodology/workflow vocabulary lock.
- [docs/models-and-storage.md](docs/models-and-storage.md) — the storage convention.

## Conventions (load-bearing, not stylistic)

**Invariants are arch tests, not prose — and the test comes first.** An
architectural invariant (an isolation, a layering rule, "X must not import Y",
"these tools don't touch disk", a fabrication pattern) belongs in an *executable*
check, not a sentence to be remembered — prose erodes silently; a failing test
blocks the PR. Write the check **before** the code it constrains: confirm it holds
on today's code, then confirm it goes *red* on a planted violation (a check that
can't fail enforces nothing). Two homes, by kind — import-graph invariants
(who-imports-whom, cycles, layers) are an `import-linter` contract in
`pyproject [tool.importlinter]` (run `PYTHONPATH=. lint-imports`); content
invariants (a call, a token, a `.get(k, <number>)` fallback — anything inside a
file) are an AST test in `tests/arch/`, kept outside the package they scan so they
never match their own source.

This is the mechanized half of the *checklist-for-human* habit: before writing a
review item for a person, ask whether an arch test can enforce it. If it can,
write the test and drop the item — CI guards it now. If it genuinely can't, it
stays a **manual-review** item. So every architectural invariant is either
*enforced* (a `tests/arch/` check, an `import-linter` contract, or a Ruff rule) or
*manual* — never merely hoped-for. Of the rules below, the blind-except ban (Ruff
`BLE001`) and the status/review isolation (an import-linter contract) are enforced;
the fabrication rule and project.py's slice of that isolation (#63) stay manual.
Prefer moving the manual ones to enforced.

- **Never fabricate.** A value that can't be sourced is `null`/`unknown`; the
  pipeline fails loudly or halts rather than inventing a number, URL, or citation.
  A requested LLM backend that isn't available raises rather than silently
  substituting another.
- **Schemas are called schemas.** A stage's `output_schema`, an input `schema:`
  block, a `TableSchema` — these are *schemas*, and that is the word, in code,
  comments, docs, and PR prose. Don't dress them up as "contracts" ("stage
  contract", "producer contract", "response contract"): the word adds no meaning
  and splits one concept across two names.
- **`human_review_queue` is how we handle asymmetrical risk.** Where a wrong
  automated result is expensive or irreversible, gate that step behind human
  sign-off: the runner halts, and decisions are content-hashed so they survive
  re-runs.
- **The status/review helpers stay below the routes layer.**
  `app/services/node_review` and `app/services/versioning` must not import
  `app.main`, `app.runtime`, or `app.compiler` — routes and templates depend on
  them, not the reverse, so the import graph stays acyclic and they stay
  unit-testable without the runtime/compiler stack. Enforced by the import-linter
  contract "Status/review services stay below the routes layer". `app/services/project`
  is meant to obey this too but still imports `app.compiler` for its
  regenerate-from-document action (`project.py:437`); #63 tracks moving that action
  into `app.services.compilation` (which wraps the compiler by design and is exempt)
  so project can rejoin the contract.
- **Never `except Exception` or bare `except`.** Catch specific exception types —
  swallowing all errors hides bugs and breaks fail-loudly. Enforced by Ruff
  `BLE001`.
