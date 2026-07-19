# Architecture — the code map

A FastAPI app over a file-backed project artifact — one directory per project (see
[overview.md](overview.md) for the layout). ~6k lines of
Python across six packages. Vocabulary: **project**/**methodology**/**workflow** per
[overview.md](overview.md).

## `app/core/models/` — the schema layer (Pydantic)
THE canonical definition of what a workflow is. Constructing a model validates it;
`validate_*` return issue lists, `parse_*` raise. **Dependency rule: imports nothing from
runtime or web — keep it pure.** Checks the *spec*, distinct from RUNTIME data validation
(`app/runtime/validation.py`, which checks dataframes).
- `stage.py` — the 8 stage types, the executable-handle block each requires, and
  `Stage.is_grain_preserving` (1:1 row correspondence — the eval gate depends on it).
- `schema.py` — `Column`, `TableSchema`, column-type vocab. `workflow.py` — graph checks
  (unique ids, inputs resolve, cycles). `named_schemas.py` — named schemas + FK `references`.
  `eval.py` — `EvalConfig` + grain-preservation gate. `table.py` — `TableRef`.

**Loading is canonical + strict.** Stages persist as JSON (`compiled/<NN>_<stage_id>.json`,
a validated `Stage`); `app/services/loader.py` is the one loader — the runner refuses a
workflow with an invalid stage (`WorkflowLoadError`), the viewer (same loader) renders
per-file issues. Typed `Stage` objects flow end-to-end.

## `app/runtime/` — the Runner  → `app/runtime/AGENTS.md`
`runner.py` — `execute_run`/`prepare_run`/`run_prepared`/`resume_run`. Per stage: validate
inputs, reject duplicate rows, dispatch, validate output, write `outputs/<stage>.parquet`,
flush `manifest.json` mid-run; halt-on-review + resume; per-run `--limit`/`--offset`;
`field_checks`. `stages/` — one module per type. `llm.py`/`options.py` — the agent
backend (no fallback). `preview.py` — scratch re-runs.

Stage handlers register under a *shape* (`app/runtime/stages/execution.py`):
`RowMapHandler` (the runtime maps a per-row function over the stage's single
input and reassembles results in input order — the function never sees the
frame), `SourceHandler` (originates rows; no upstream frames), or
`FrameHandler` (whole frames; may reshape). The shape fixes what the runtime
hands the handler, so grain-and-order preservation is structural for
row-mapped types rather than declared per stage. The preservation fact itself is
owned by core (`app.core.models.stage.is_grain_and_order_preserving`) so every layer
can read it; `check_registry_matches_model` raises at registry import if any
type's registered shape disagrees with that core fact, and
`tests/test_handler_registry.py` pins the same per-type equality in CI.

## `app/compiler/` — prose → LLM → workflow engine
Public surface `read_input` + `compile_methodology`. Validates the reply against the models
and **re-asks on schema-validation failure**, not just parse failure. CLI `python -m
app.compiler`; persistence in `app/services/compilation.py`. Authoring UI not on master yet.

## `app/web/` — the web layer  → `app/AGENTS.md`
Thin `app/main.py` (~40 lines); routes under `/project/{project}/…`. Routers: `project.py`
(index, workflow graph, stage detail, ER), `runs.py` (trigger/list/detail/status-poll, rows
+ CSV, scratch preview, resume), `review.py` (review queue), `node_review.py` (node approval
+ editing + version creation — the only writer to `compiled/`). `web/{config,loading,
diagrams}.py` — paths + Jinja · viewer reads over the loader · mermaid/ER builders.

## `app/services/` — web-independent workflow logic
`loader.py` (canonical stage loader, above); `compilation.py` (compile persistence for
`app/compiler`); `node_review.py` (content-hash approval over stage specs — read its
docstring; the canonical-hash invariant must not rot); `versioning.py` (freeze compiled
stages + schemas into a `Version` document in the store).

## `app/chat/`, `app/core/llm/`, tests
`chat/` — a reusable PydanticAI chat engine (streaming, tools, file persistence), separate
from the row-mapped `llm_transform` path; own env (`CW_CHAT_BACKEND`); one demo tool, not yet
wired in. `core/llm/options.py` — the `LLMModel` menu. `tests/` (pytest; `conftest.py` forces
`agent_available` False so no test can reach a real model); `.github/workflows/ci.yml`
runs ruff + mypy + pytest on every PR.
