# Data model (Pydantic) + storage convention

## Data model — `app/models.py` — IMPLEMENTED

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
  compiled DAGs (weighting is done inside `python_transform` modules).

**Not done yet (next step):** the runtime (`runner.py`, `main.py`) still reads
stage dicts directly and does not yet parse them through these models, so the
stage-spec contract is still not *enforced at run time*. Wiring `parse_methodology`
/ `validate_methodology` into the loader is the follow-up — kept separate because
the real compiled DAGs may surface issues that need triage.

## Storage convention — `<object_type>/<object_id>.data` — DECIDED, NOT YET IMPLEMENTED

Objects are stored on disk as `<object_type>/<object_id>.data`, with a **uniform
`.data` extension** for consistency (e.g. `dag/<dag_id>.data`, `run/<run_id>.data`,
`decision/<decision_id>.data`). "We're not making a DB, but we still follow a clean
`<object_type>/<object_id>` object store."

Current layout is `examples/<name>/{compiled, runs/<id>/…, decisions, data, code, stages}`.
Migrating to the new convention touches the runner's output paths, `main.py`'s
loader, and moves existing example runs — so it's **deferred to a separate, reviewed
change**, not bundled with the model work.
