# Architecture — the code map

A FastAPI app over a file-backed project artifact. No database; everything is
files under `examples/<name>/` (untracked runtime data — see
[overview.md](overview.md) for the project directory layout). As of this
writing the app is ~6k lines of Python across six packages.

Vocabulary used below (locked; see [naming-refactor.md](naming-refactor.md)):
**project** = the container directory · **methodology** = the authored prose ·
**workflow** = the compiled, executable stage graph.

## `app/models/` — the schema layer (Pydantic)

THE canonical definition of what a workflow is, as a Pydantic package.
Constructing a model validates it; `validate_*` helpers return non-fatal issue
lists; `parse_*` raise. **Dependency rule (deliberate): it imports nothing from
the runtime or the web layer.** Keep it pure.

What lives where:
- `models/stage.py` — the **8 stage types** (`input_data`, `llm_transform`,
  `python_row_function`, `python_frame_function`, `join`, `aggregate`,
  `human_review_queue`, `publish`), the executable-handle block each requires
  (`connector`/`llm`/`function`/`join`/`aggregate`/`queue`/`publish`), and
  `Stage.is_grain_preserving` (whether a stage keeps a 1:1 row correspondence —
  the eval gate depends on it). Prefer `python_row_function` (the runtime maps
  it row-by-row, so it *cannot* fan out/in) over `python_frame_function` unless
  the logic needs the whole frame.
- `models/schema.py` — primitives: `Column`, `TableSchema`, the column-type
  vocabulary (`SCALAR_COLUMN_TYPES`).
- `models/workflow.py` — the graph-level checks: unique ids, inputs resolve,
  cycle detection; `validate_workflow` / `parse_workflow`.
- `models/named_schemas.py` — named schemas: `SchemaKind`, `NamedColumn` (with
  `references: <schema>[.<column>]` foreign keys), `SchemaLibrary`. See
  [named-schemas.md](named-schemas.md).
- `models/eval.py` — the eval data model: `EvalConfig`, `EvalRun`,
  `resolve_eval_run_settings` (the grain-preservation gate). See
  [named-schemas.md](named-schemas.md#the-eval-data-model).
- `models/table.py` — `TableRef`, a pointer to a stored table.

**Loading is canonical and strict.** Compiled stages persist as JSON
(`compiled/<NN>_<stage_id>.json`, the dump of a validated `Stage`), and
`app/services/loader.py` is the one loader: the runner refuses to execute a
workflow with an invalid stage (`WorkflowLoadError`), and the viewer
(`app/web/loading.py`, built on the same loader) renders per-file issues
instead of a graph with holes. Typed `Stage` objects flow end-to-end.

Distinct from RUNTIME DATA validation (`app/runtime/validation.py`), which
checks actual dataframes against a schema at run time. `app/models/` checks the
*spec*. Note: `Stage.output_schema` is optional (legitimately so for `publish`);
a table-producing stage that omits it runs with only a warning — issue #51
tracks resolving this via a discriminated union.

## `app/runtime/` — the Runner

Executes a workflow. → `app/runtime/AGENTS.md` is the detailed doc; highlights:
- `runner.py` — `execute_run` / `prepare_run` / `run_prepared` / `resume_run`.
  Per stage: validate inputs, reject duplicate input rows, dispatch to the
  handler, validate output, write `outputs/<stage>.parquet`, flush
  `manifest.json` **mid-run** (so the UI shows live progress). Halt-on-review +
  resume; per-run `--limit`/`--offset` row slicing; LLM `field_checks`
  (validate-and-retry on declared per-field rules).
- `stages/` — one module per stage type (`input_data`, `llm_transform`,
  `python_functions` for the two python types, `join`, `aggregate`,
  `human_review_queue`, `publish`), shared helpers in `_shared.py`.
- `llm.py` / `llm_agent_sdk.py` / `llm_mock.py` / `options.py` — LLM backends.
  `CW_LLM_BACKEND=agent_sdk|cli|mock` (default `auto`: agent_sdk → cli). The
  mock is opt-in via `CW_LLM_FORCE_MOCK=1`; with no live backend the run fails
  loudly rather than silently mocking.
- `validation.py` — dataframe-vs-schema checks. `preview.py` — ephemeral
  in-memory scratch re-runs for the UI.

## `app/compiler/` — the prose → LLM → workflow authoring engine

Public surface: `read_input` + `compile_methodology` (the LLM call, validation
pass, and prompt builders are package-internal). Validates the LLM's reply
against the models and **re-asks on schema-validation failure**, not just parse
failure. CLI: `python -m app.compiler`. Persisting a compile as a first-class
object is owned by `app/services/compilation.py`. The authoring *UI* (compile
pages) is not on master yet.

## `app/web/` — the web layer

`app/main.py` is a thin bootstrap (~40 lines): creates the FastAPI app, mounts
`/static`, includes the routers. Routes live under `/project/{project}/…`:
- `web/routers/project.py` — index, workflow graph view, stage detail, ER
  data-model view.
- `web/routers/runs.py` — trigger/list/detail/status-poll, stage panel,
  full-table rows view + CSV download, scratch preview, artifacts, resume.
- `web/routers/review.py` — the human-review queue UI + decision persistence.
- `web/routers/node_review.py` — per-node belief approval, node editing, and
  workflow version creation (the only writer to `compiled/`).
- `web/{config,loading,diagrams}.py` — paths + Jinja singleton · viewer-side
  reads over the canonical loader · mermaid/ER builders (pure, no I/O).

→ `app/AGENTS.md` documents the routes and the stage-panel UI in detail.

## `app/services/` — web-independent workflow logic

- `loader.py` — the canonical compiled-stage loader (see above).
- `compilation.py` — compile persistence (manifest, what-happened, workflow on
  disk) for `app/compiler`.
- `node_review.py` — content-hash approval state over stage specs. Read its
  docstring before touching the loader: the canonical-hash invariant is the one
  correctness rule that must not rot.
- `versioning.py` — freeze `compiled/` into `versions/<version_id>/` with
  approval coverage recorded in `version.json`.

## `app/chat/` — embeddable chat subsystem

A reusable PydanticAI chat engine (streaming, thinking events, pluggable tools,
file-based session persistence) mounted into the app. Deliberately separate from
the `llm_transform` batch path — this is the interactive, multi-turn surface.
Its own backend selection (`CW_CHAT_BACKEND`: dev / claude_cli / anthropic) is
separate from the runtime's `CW_LLM_*` namespace. Currently exposes one demo
tool (list projects); it is not yet wired into authoring or review workflows.

## `app/llm/` — shared LLM vocabulary

`options.py`: the `LLMModel` enum — the menu of models a stage may name.

## Tests + CI

`tests/` (pytest; `conftest.py` forces `CW_LLM_FORCE_MOCK=1` so the suite is
offline). Strong coverage on `app/models`; thinner on the runtime; none yet on
most web routers or chat. `.github/workflows/ci.yml` runs ruff, mypy, and
pytest on every PR; dev pins live in `requirements-dev.txt`.

## Running it

```
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8765     # web UI
python -m app.runtime.runner examples/<name>   # run a project's workflow from the CLI
```
