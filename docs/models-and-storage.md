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

**Two kinds of persistence, and no third.** Every record is a JSON document in
the SQLite key-value store (`app/core/persistence.py`), keyed by
`(collection, id)`; every dataframe is parquet under the frame store
(`app/core/frames.py`). `tests/arch/test_persistence_is_frames_and_the_store.py`
fails on any other write under `app/` — the exemptions are an export the user
downloads, a file the user uploaded, and a publish stage's own artifact.

| Collection | Id | Owner |
|---|---|---|
| `project` | project name | `app/services/project.py` |
| `methodology` | project name | `app/services/methodology.py` |
| `data_model` | project name | `app/services/data_model.py` |
| `working_copy` | project name | `app/services/loader.py` |
| `workflow_version`, `review_guide` | `<project>/<version_id>` | `app/services/versioning.py` |
| `draft` | draft id | `app/services/drafts.py` |
| `run` | `<project>/<run_id>` | `app/runtime/manifest.py` |
| `run_events` | `<project>/<run_id>/<chunk>` | `app/runtime/run_log.py` |
| `queue_fingerprints` | `<project>/<run_id>/<stage_id>` | `app/runtime/stages/human_review_queue.py` |
| stage-result cache | see the module | `app/core/stage_cache.py` |

A project's directory survives for what is genuinely file-shaped: input data the
user uploaded, `code/` modules a stage imports, and `runs/<id>/` holding stage
output frames, lineage sidecars and published artifacts.
