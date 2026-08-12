# Architecture — the code map

A FastAPI app over a file-backed project artifact — one directory per project (see
[overview.md](overview.md) for the layout). ~6k lines of
Python across six packages. Vocabulary: **project**/**methodology**/**workflow** per
[overview.md](overview.md).

Two entrypoints sit above the packages, importing them and imported by nothing:
`app/main.py` (the ASGI app — `python -m uvicorn app.main:app`) and `app/cli.py`
(`python -m app.cli <project>` — one run of a project's newest stored version,
driven through `app/services/run.py`).

## `app/models/` — the schema layer (Pydantic)
THE definition of what a workflow is. Constructing a model validates it;
`validate_*` return issue lists, `parse_*` raise. **Dependency rule: imports nothing from
runtime or web — keep it pure.** Checks the *spec*, distinct from RUNTIME data validation
(`app/runtime/validation.py`, which checks dataframes).
- `stage.py` — `Stage`, the pydantic discriminated union over the per-type models keyed on
  `type` (parse a stage dict with `parse_stage`; `Stage` is an annotation, not a class), and
  `StageDraft`, the flat all-optional shape an authoring client submits. What the
  `add_stage` tools actually bind is `SubmittedStage` (`app/tools/submitted_stage.py`),
  which trims the server-owned fields a client echoes back before the draft sees them.
- `stages/stage_base.py` — the stage types, and `AbstractStage`: the fields and rules every
  stored stage satisfies whatever its type, plus `is_grain_and_order_preserving` (1:1 row
  correspondence in order — the eval gate depends on it).
- `stages/signature.py` — `TransformSignature`, the contract every stored stage declares
  about what it reads and writes. Form `extends`: output is the first input's rows plus
  `rewrites` (revised in place) and `adds` (new columns), every other anchor column
  flowing through untouched. Form `replaces`: nothing flows, output is exactly
  `produces`. `reads` names what the transform consumes per input — a column that merely
  passes through is not a read. A stage may be TOLD a schema and never holds one:
  `promised_output_schema(stage, inputs)` computes the output from the signature and the
  input schemas `Workflow` resolved.
- `stages/` — one module per stage type alongside `stage_base.py`, holding that type's
  config class, its `AbstractStage` subclass (which declares the blocks that type REQUIRES and
  its input arity), and its own validation helpers. `PythonFunction` and both
  python-transform stage models live in `stages/code.py`; `StarlarkFunction` and
  `StarlarkRowFunctionStage` live in `stages/starlark.py`.
- `schema.py` — `Column`, `TableSchema`, column-type vocab. `workflow.py` — graph checks
  (unique ids, inputs resolve, cycles) and schema RESOLUTION: a stage's input and output
  schemas are a function of the whole graph, so `Workflow` walks it in dependency order
  and hands out `WorkflowStage` (`workflow_stage.py`) — the authored `Stage` plus what
  each input supplies and what the stage emits. Every consumer that needs a schema takes
  a `WorkflowStage`; the read/write seam (loader, stage_edit, drafts, versioning, seeds,
  tools, mcp, agents, compiler) still names `Stage`. `named_schemas.py` — named schemas +
  FK `references`. `eval.py` — `EvalConfig` + grain-preservation gate. `table.py` — `TableRef`.

**Loading is normalizing + strict.** Stages persist as JSON (`compiled/<NN>_<stage_id>.json`,
a validated `Stage`); `app/services/loader.py` is the one loader — the runner refuses a
workflow with an invalid stage (`WorkflowLoadError`), the viewer (same loader) renders
per-file issues. Typed `Stage` objects flow end-to-end.

## `app/runtime/` — the Runner  → `app/runtime/AGENTS.md`
`runner.py` — `execute_run`/`prepare_run`/`run_prepared`/`resume_run`, each taking the
stages of the version the run pins. The runner reads no versions: the caller resolves one
(`app/services/versioning.py: resolve_version_id`, defaulting to the newest STORED version,
published or not — never the working copy, never a draft), loads its frozen stages and
hands them in. `app/services/run.py` is the one place that composes this, and an
import-linter contract keeps `runner.py` free of `app.services` so the arrow between the two
points one way; `app/cli.py` drives that same seam. Per stage: validate
inputs, reject duplicate rows, dispatch, validate output, write `outputs/<stage>.parquet`,
flush `manifest.json` mid-run; halt-on-review + resume; per-run `--limit`/`--offset`
capping the rows a stage READS (cut off its inputs before its handler runs);
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
Three generators, each an `app.core.agent` Agent targeting a model schema: `data_model.py`
(document → `SchemaLibrary`, the nouns the workflow is built on), `stage_tests.py` (one
python-transform stage + the document → its `StageTest` cases, generated code-blind), and
`review_guide.py` (one saved version's frozen stages + the document → its `ReviewGuide`).
All three submit through `submit_answer`, so a schema-invalid reply is **re-asked inside
the agent's own loop**, not just parse-checked. `app/services/generation.py` drives them
and persists what comes back. The guide author is given the version's stages and no tool
that reads a project, so it cannot narrate the working copy the version was cut from.
Workflow stages are authored one at a time through `app/services/stage_edit.py`.

## `app/web/` — the web layer  → `app/AGENTS.md`
Thin `app/main.py` (~40 lines); routes under `/project/{project}/…`. Routers: `project.py`
(index, workflow graph, stage detail, ER, plus the version-first IA — `/workflow/versions`
lists every version newest-first, `/workflow/version/{id}` is one immutable version's
read-only detail with Publish/Run-this-version; the mutable editor stays at `/workflow`),
`runs.py` (trigger/list/detail/status-poll, rows + CSV, scratch preview, resume, plus
running one specific pinned version), `review.py` (review queue), `node.py` (the per-node
panel + spec editing + version creation + publish — the only writer to `compiled/`),
`guide.py` (`POST /workflow/version/{id}/guide` — starts review-guide authoring for one
version, watched through node.py's generation-session status endpoint).
`web/{config,loading,diagrams}.py` — paths + Jinja · viewer reads over the loader ·
mermaid/ER builders. `web/{run_header,run_index,run_issues,stage_strip}.py` build what the run
page and the runs index show about a run: the header's grounding line and its single
state-chosen action, the index rows, the issue index above the graph (what stopped the run,
then every issue that did not — see `app/AGENTS.md`), and the per-stage status strip both
pages draw.
Everything a run page states about the workflow — its graph, each
stage's source and schemas, the lineage panel, and the scratch re-run's handler — is read
from the version its manifest pinned to (`run.load_run_stages` /
`run.load_pinned_stage_def` in `app/services/`), never from `compiled/`; a manifest naming no resolvable version raises
`RunVersionUnresolvableError`, and the page shows an unavailable notice instead of the
working copy while the scratch re-run refuses to execute (409).

## `app/services/` — web-independent workflow logic
`run.py` (the production run seam — start/execute/resume/status, and the only module that
drives `app/runtime/runner.py`: it resolves the version and loads its stages before handing
them to the runner, plus resolves what a run pinned — `resolve_version`,
`read_pinned_version`, `load_run_stages`, `load_pinned_stage_def`); `loader.py` (stage loader, above); `compilation.py` (compile persistence for
`app/compiler`); `versioning.py` (`create_version_from_stages`
is the ONE write path for a `WorkflowVersion` document, born unpublished; `publish_version`
is the metadata-only record that a human reviewed a version — a signal about it, which
`resolve_version_id` does not read); `drafts.py` (disposable, mutable scratch — a `Draft` document
that may be invalid mid-edit, edited only through the editing agent's tools; `save_version`
is its only exit, strict-validating before freezing it into a version via
`create_version_from_stages`).

## `app/chat/`, `app/core/llm/`, tests
`chat/` — a reusable PydanticAI chat engine (streaming, tools, file persistence), separate
from the row-mapped `llm_transform` path; one demo tool, not yet wired in.
`core/llm/options.py` — the `LLMModel` menu. `tests/` (pytest; `conftest.py` forces
`agent_available` False so no test can reach a real model); `.github/workflows/ci.yml`
runs ruff + mypy + pytest on every PR.
