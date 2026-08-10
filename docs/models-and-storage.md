# Data model (Pydantic) + storage convention

## Data model — `app/models/` — IMPLEMENTED

The workflow contract is a Pydantic package: `Stage` (a discriminated union over one
model per stage type — parse with `parse_stage`), `Workflow`, and the config blocks
(`Connector`, `LLMConfig`, `PythonFunction`, `JoinConfig`,
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
- Connector kinds reduced to the implemented `file`. The rest
  (`http`/`scrape`/`api`/`manual_upload`/`sql`) were declared but never had a
  handler — add them back alongside a handler.
- Weighted aggregation formulas (`weighted_mean`/`weighted_sum`) — unused in the
  compiled workflows (weighting is done inside `python_frame_function` modules).

**Enforced at load, via `app/services/loader.py`.** This is the only place that
reads a project's `working_copy` document (one record per project, keyed by
project name, holding the ordered stage list); everything past it speaks `Stage`
objects, not dicts. Two entry points, both parsing each spec through
`parse_stage`:
- `load_workflow` — strict, for the runner. Any invalid stage or
  cross-stage issue raises `WorkflowLoadError`, and the runner refuses to
  execute the workflow.
- `load_stage_entries` — tolerant, per-stage, for the viewer. Each stored spec
  gets a `StageEntry` (parsed `Stage` or `None` + an issues list). If any is
  invalid, the viewer surfaces the issues and renders no workflow at all
  (a partial graph with holes would mislead) instead of crashing.

`app/runtime/handlers.py`, `runner.py`, `preview.py`, and the web layer all consume
the typed `Stage` objects this loader returns.

## Storage

Every record is a JSON document in the SQLite key-value store
(`app/core/persistence.py`), keyed by `(collection, id)`. Frames are parquet.
`app/services/loader.py` owns `working_copy`, `app/services/data_model.py` owns
`data_model`, `app/services/versioning.py` owns `workflow_version` and
`review_guide`, `app/services/drafts.py` owns `draft`, `app/services/project.py`
owns `project`, and `app/core/stage_cache.py` owns the stage-result cache.
