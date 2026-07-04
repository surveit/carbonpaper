# Architecture — the code map

A FastAPI app over a file-backed methodology artifact. No database; everything is
files under `examples/<name>/`. As of this writing the app is ~5.7k lines of
Python across five packages.

## `app/models/` — the schema layer (Pydantic)

THE canonical definition of what a methodology is, as a Pydantic package.
Constructing a model validates it; `validate_*` helpers return non-fatal issue
lists; `parse_*` raise. **Dependency rule (deliberate): it imports nothing from
the runtime or the web layer.** Keep it pure.

What lives where:
- `models/stage.py` — the **7 stage types** (`input_data`, `llm_transform`,
  `python_transform`, `join`, `aggregate`, `human_review_queue`, `publish`), the
  executable-handle block each requires (`connector`/`llm`/`function`/`join`/
  `aggregate`/`queue`/`publish`), and `Stage.is_grain_preserving` (whether a
  stage keeps a 1:1 row correspondence — the eval gate depends on it).
- `models/schema.py` — primitives: `Column`, `TableSchema`, the column-type
  vocabulary (`SCALAR_COLUMN_TYPES`).
- `models/methodology.py` — the DAG-level checks: unique ids, inputs resolve,
  cycle detection; `validate_methodology` / `parse_methodology`.
- `models/named_schemas.py` — named schemas: `SchemaKind`, `NamedColumn` (with
  `references: <schema>[.<column>]` foreign keys), `SchemaLibrary`. See
  [named-schemas.md](named-schemas.md).
- `models/eval.py` — the eval data model: `EvalConfig`, `EvalRun`,
  `resolve_eval_run_settings` (the grain-preservation gate). See
  [named-schemas.md](named-schemas.md#the-eval-data-model).
- `models/table.py` — `TableRef`, a pointer to a stored table.
- `app/SCHEMA.md` is the legacy prose spec (superseded by `app/models/`).

**Caveat (honest state, not aspiration):** the runtime and the web layer do NOT
yet parse stage dicts through these models — YAML is loaded as raw dicts in three
separate places (`app/runtime/runner.py`, `app/web/loading.py`,
`app/services/versioning.py`). Wiring the loaders through `parse_methodology` is
the agreed next step; see [models-and-storage.md](models-and-storage.md).

Distinct from RUNTIME DATA validation (`app/runtime/validation.py`), which checks
actual dataframes against a schema at run time. `app/models/` checks the *spec*.

## `app/runtime/` — the Runner

Executes a DAG. → `app/runtime/AGENTS.md` is the detailed doc; highlights:
- `runner.py` — `execute_run` / `prepare_run` / `run_prepared` / `resume_run`.
  Per stage: validate inputs, reject duplicate input rows, dispatch to the
  handler, validate output, write `outputs/<stage>.parquet`, flush
  `manifest.json` **mid-run** (so the UI shows live progress). Halt-on-review +
  resume; per-run `--limit`/`--offset` row slicing; LLM `field_checks`
  (validate-and-retry on declared per-field rules).
- `handlers.py` — one handler per stage type (`HANDLERS` dict).
- `llm.py` / `llm_agent_sdk.py` / `llm_mock.py` / `options.py` — LLM backends.
  `CW_LLM_BACKEND=agent_sdk|cli|mock` (default `auto`: agent_sdk → cli). The
  mock is opt-in via `CW_LLM_FORCE_MOCK=1`; with no live backend the run fails
  loudly rather than silently mocking.
- `validation.py` — dataframe-vs-schema checks. `preview.py` — ephemeral
  in-memory scratch re-runs for the UI.

## `app/web/` — the web layer

`app/main.py` is a thin bootstrap (~40 lines): creates the FastAPI app, mounts
`/static`, includes the routers. The layer proper:
- `web/routers/methodology.py` — index, DAG view, stage detail, ER data-model view.
- `web/routers/runs.py` — trigger/list/detail/status-poll, stage panel, full-table
  rows view + CSV download, scratch preview, artifacts, resume.
- `web/routers/review.py` — the human-review queue UI + decision persistence.
- `web/routers/node_review.py` — per-node belief approval, node editing, and DAG
  version creation (the only writer to `compiled/`).
- `web/{config,loading,diagrams}.py` — paths + Jinja singleton · filesystem reads
  & stage-dict helpers · mermaid/ER builders (pure, no I/O).

→ `app/AGENTS.md` documents the routes and the stage-panel UI in detail.

## `app/services/` — web-independent workflow logic

- `node_review.py` — content-hash approval state over stage dicts. Read its
  docstring before touching any loader: the canonical-hash invariant (hash the
  LOADED dict minus the bookkeeping keys `_filename`/`_order`/`_error`) is the
  one correctness rule that must not rot.
- `versioning.py` — freeze `compiled/` into `versions/<version_id>/` with
  approval coverage recorded in `version.json`.

Both import only stdlib + yaml + pandas — no runtime, no web.

## `app/chat/` — embeddable chat subsystem

A reusable PydanticAI chat engine (streaming, thinking events, pluggable tools,
file-based session persistence) mounted into the app. Deliberately separate from
the `llm_transform` batch path — this is the interactive, multi-turn surface. Its
own backend selection (`CW_CHAT_BACKEND`: dev / claude_cli / anthropic) is
separate from the runtime's `CW_LLM_*` namespace. Currently exposes one demo tool
(list methodologies); it is not yet wired into authoring or review workflows.

## `app/llm/` — shared LLM vocabulary

`options.py`: the `LLMModel` enum — the menu of models a stage may name.

## Tests + CI

`tests/` (pytest; `conftest.py` forces `CW_LLM_FORCE_MOCK=1` so the suite is
offline). Strong coverage on `app/models`; thinner on the runtime (one runner
integration test + backend-selection and JSON-parsing units); none yet on the web
routers (except the rows view) or chat. `.github/workflows/ci.yml` runs ruff,
mypy, and pytest on every PR; dev pins live in `requirements-dev.txt`.

## Running it

```
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8765     # web UI
python -m app.runtime.runner examples/<name>   # run a methodology from the CLI
```
