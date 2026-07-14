# Data model (Pydantic) + storage convention

## Data model — `app/core/models/` — IMPLEMENTED

The workflow contract is a single Pydantic module: `Stage`, `Workflow`,
and the handle blocks (`Connector`, `LLMConfig`, `PythonFunction`, `JoinConfig`,
`AggregateConfig`, …) plus `Column` / `TableSchema`. **Constructing a model
validates it** — the model *is* the contract, so there's no separate validator to
keep in sync.

This **replaces and removes** two older things:
- `app/schema.py` — a dataclass *spec* that was imported by nothing.
- `app/dag_schema.py` — hand-rolled validators returning issue-string lists.

Convenience entry points remain for the non-fatal, "show the user the problems"
case: `validate_workflow(stages) -> list[str]` and `validate_stage(stage) -> list[str]`
(empty list = valid). `parse_workflow(stages) -> Workflow` raises instead.

**Cut in this change (per review):**
- Connector kinds reduced to the implemented `file` + `computed_static`. The rest
  (`http`/`scrape`/`api`/`manual_upload`/`sql`) were declared but never had a
  handler — add them back alongside a handler.
- Weighted aggregation formulas (`weighted_mean`/`weighted_sum`) — unused in the
  compiled workflows (weighting is done inside `python_frame_function` modules).

**Enforced at load, via `app/services/loader.py`.** This is the only place that
reads the on-disk compiled-stage JSON (`compiled/<NN>_<stage_id>.json`, the JSON
dump of the validated `Stage` model; the `NN_` prefix orders the stage list in
the UI); everything past it speaks `Stage` objects, not dicts. Two entry points,
both parsing each file through `Stage.model_validate`:
- `load_workflow` — strict, for the runner. Any invalid stage or
  cross-stage issue raises `WorkflowLoadError`, and the runner refuses to
  execute the workflow.
- `load_compiled_dir` — tolerant, per-file, for the viewer. Each compiled file
  gets a `CompiledStageFile` (parsed `Stage` or `None` + an issues list). If any
  file is invalid, the viewer surfaces the issues and renders no workflow at all
  (a partial graph with holes would mislead) instead of crashing.

`app/runtime/handlers.py`, `runner.py`, `preview.py`, and the web layer all consume
the typed `Stage` objects this loader returns.

## Storage convention — `<object_type>/<object_id>.data` — DECIDED, NOT YET IMPLEMENTED

Objects are stored on disk as `<object_type>/<object_id>.data`, with a **uniform
`.data` extension** for consistency (e.g. `workflow/<workflow_id>.data`, `run/<run_id>.data`,
`decision/<decision_id>.data`). "We're not making a DB, but we still follow a clean
`<object_type>/<object_id>` object store."

Current layout is `examples/<name>/{compiled, runs/<id>/…, decisions, data, code, stages}`.
Migrating to the new convention touches the runner's output paths, `main.py`'s
loader, and moves existing example runs — so it's **deferred to a separate, reviewed
change**, not bundled with the model work.
