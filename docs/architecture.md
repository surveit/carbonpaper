# Architecture — the code map

A FastAPI app over a file-backed project artifact — one directory per project (see
[overview.md](overview.md) for the layout). ~6k lines of
Python across six packages. Vocabulary: **project**/**methodology**/**workflow** per
[overview.md](overview.md).

## `app/models/` — the schema layer (Pydantic)
THE definition of what a workflow is. Constructing a model validates it;
`validate_*` return issue lists, `parse_*` raise. **Dependency rule: imports nothing from
runtime or web — keep it pure.** Checks the *spec*, distinct from RUNTIME data validation
(`app/runtime/validation.py`, which checks dataframes).
- `stage_base.py` — the stage types, and `StageBase`: the fields and rules every stored
  stage satisfies whatever its type, plus `is_grain_and_order_preserving` (1:1 row
  correspondence in order — the eval gate depends on it).
- `stage.py` — `Stage`, the pydantic discriminated union over the per-type models keyed on
  `type` (parse a stage dict with `parse_stage`; `Stage` is an annotation, not a class), and
  `StageDraft`, the flat all-optional shape an authoring client submits.
- `stages/` — one module per stage type, holding that type's config class, its `StageBase`
  subclass (which declares the blocks that type REQUIRES and its input arity), and its own
  validation helpers. `PythonFunction` and both python-transform stage models live in
  `stages/code.py`.
- `schema.py` — `Column`, `TableSchema`, column-type vocab. `workflow.py` — graph checks
  (unique ids, inputs resolve, cycles). `named_schemas.py` — named schemas + FK `references`.
  `eval.py` — `EvalConfig` + grain-preservation gate. `table.py` — `TableRef`.

**Loading is normalizing + strict.** Stages persist as JSON (`compiled/<NN>_<stage_id>.json`,
a validated `Stage`); `app/services/loader.py` is the one loader — the runner refuses a
workflow with an invalid stage (`WorkflowLoadError`), the viewer (same loader) renders
per-file issues. Typed `Stage` objects flow end-to-end.

## `app/runtime/` — the Runner  → `app/runtime/AGENTS.md`
`runner.py` — `execute_run`/`prepare_run`/`run_prepared`/`resume_run`; every run pins to a
PUBLISHED workflow version (`resolve_version_id`, defaulting to the newest published one) —
never the working copy, never a draft, never an unpublished version. Per stage: validate
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
owned by the domain models (`app.models.stage.is_grain_and_order_preserving`) so every layer
can read it; `validate_registry_matches_model` raises at registry import if any
type's registered shape disagrees with that core fact, and
`tests/test_handler_registry.py` pins the same per-type equality in CI.

## `app/compiler/` — prose → LLM generation engines
Two generators, each an `app.core.agent` Agent targeting a model schema: `data_model.py`
(document → `SchemaLibrary`, the nouns a human then approves) and `stage_tests.py` (one
python-transform stage + the document → its `StageTest` cases, derived code-blind). Both
submit through `submit_answer`, so a schema-invalid reply is **re-asked inside the agent's
own loop**, not just parse-checked. `app/services/generation.py` drives them and persists
what comes back. Workflow stages are authored one at a time through `app/services/stage_edit.py`.

## `app/web/` — the web layer  → `app/AGENTS.md`
Thin `app/main.py` (~40 lines); routes under `/project/{project}/…`. Routers: `project.py`
(index, workflow graph, stage detail, ER, plus the version-first IA — `/workflow/versions`
lists every version newest-first, `/workflow/version/{id}` is one immutable version's
read-only detail with Publish/Run-this-version; the mutable editor stays at `/workflow`),
`runs.py` (trigger/list/detail/status-poll, rows + CSV, scratch preview, resume, plus
running one specific pinned version), `review.py` (review queue), `node_review.py` (node
approval + editing + version creation + publish — the only writer to `compiled/`).
`web/{config,loading,diagrams}.py` — paths + Jinja · viewer reads over the loader ·
mermaid/ER builders. Everything a run page states about the workflow — its graph, each
stage's source and schemas, the lineage panel, and the scratch re-run's handler — is read
from the version its manifest pinned to (`run.load_run_stages` /
`run.load_pinned_stage_def` in `app/services/`), never from `compiled/`; a manifest naming no resolvable version raises
`RunVersionUnresolvableError`, and the page shows an unavailable notice instead of the
working copy while the scratch re-run refuses to execute (409).

## `app/services/` — web-independent workflow logic
`run.py` (the production run seam — start/resume/status, plus resolving what a run
pinned: `resolve_version`, `load_run_stages`, `load_pinned_stage_def`); `loader.py` (stage loader, above); `compilation.py` (compile persistence for
`app/compiler`); `node_review.py` (content-hash approval over stage specs — read its
docstring; the content-hash invariant must not rot); `versioning.py` (`create_version_from_stages`
is the ONE write path for a `WorkflowVersion` document, born unpublished; `publish_version`
is the metadata-only human-approval act a run's `resolve_version_id` requires before it
will pin to that version); `drafts.py` (disposable, mutable scratch — a `Draft` document
that may be invalid mid-edit, edited only through the editing agent's tools; `save_version`
is its only exit, strict-validating before freezing it into a version via
`create_version_from_stages`).

## `app/chat/`, `app/core/llm/`, tests
`chat/` — a reusable PydanticAI chat engine (streaming, tools, file persistence), separate
from the row-mapped `llm_transform` path; one demo tool, not yet wired in.
`core/llm/options.py` — the `LLMModel` menu. `tests/` (pytest; `conftest.py` forces
`agent_available` False so no test can reach a real model); `.github/workflows/ci.yml`
runs ruff + mypy + pytest on every PR.
