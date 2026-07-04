# Data model (Pydantic) + storage convention

## Data model — `app/models/` — IMPLEMENTED

The methodology DAG contract is a single Pydantic module: `Stage`, `Methodology`,
and the handle blocks (`Connector`, `LLMConfig`, `PythonFunction`, `JoinConfig`,
`AggregateConfig`, …) plus `Column` / `TableSchema`. **Constructing a model
validates it** — the model *is* the contract, so there's no separate validator to
keep in sync.

This **replaces and removes** two older things:
- `app/schema.py` — a dataclass *spec* that was imported by nothing.
- `app/dag_schema.py` — hand-rolled validators returning issue-string lists.

Convenience entry points remain for the non-fatal, "show the user the problems"
case: `validate_methodology(stages) -> list[str]` and `validate_stage(stage) -> list[str]`
(empty list = valid). `parse_methodology(stages) -> Methodology` raises instead.

**Cut in this change (per review):**
- Connector kinds reduced to the implemented `file` + `computed_static`. The rest
  (`http`/`scrape`/`api`/`manual_upload`/`sql`) were declared but never had a
  handler — add them back alongside a handler.
- Weighted aggregation formulas (`weighted_mean`/`weighted_sum`) — unused in the
  compiled DAGs (weighting is done inside `python_frame_function` modules).

**Enforced at load, via `app/services/loader.py`.** This is the only place that
reads the on-disk compiled-stage YAML; everything past it speaks `Stage` objects,
not dicts. Two entry points, both parsing each file through `Stage.model_validate`:
- `load_methodology_stages` — strict, for the runner. Any invalid stage or
  cross-stage issue raises `MethodologyLoadError`, and the runner refuses to
  execute the DAG.
- `load_compiled_dir` — tolerant, per-file, for the viewer. Each compiled file
  gets a `CompiledStageFile` (parsed `Stage` or `None` + an issues list), and the
  web UI renders any issues as a banner instead of crashing.

`app/runtime/handlers.py`, `runner.py`, `preview.py`, and the web layer all consume
the typed `Stage` objects this loader returns.

## Storage convention — `<object_type>/<object_id>.data` — DECIDED, NOT YET IMPLEMENTED

Objects are stored on disk as `<object_type>/<object_id>.data`, with a **uniform
`.data` extension** for consistency (e.g. `dag/<dag_id>.data`, `run/<run_id>.data`,
`decision/<decision_id>.data`). "We're not making a DB, but we still follow a clean
`<object_type>/<object_id>` object store."

Current layout is `examples/<name>/{compiled, runs/<id>/…, decisions, data, code, stages}`.
Migrating to the new convention touches the runner's output paths, `main.py`'s
loader, and moves existing example runs — so it's **deferred to a separate, reviewed
change**, not bundled with the model work.
