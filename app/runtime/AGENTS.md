# app/runtime — the Runner (workflow executor)

Executes a workflow and persists the result. Does not import the compiler or the web app.
Loads stages through `app/services/loader.py: load_workflow`, which parses each compiled
stage spec into a `Stage` and raises `WorkflowLoadError` if any stage or cross-stage check
fails — an invalid workflow is refused before the runner does any work.

## `runner.py` — the executor
`topological_sort` → `execute_run(project_dir, repo_root)`. Per stage: validate declared
inputs (`validation.py`), reject duplicate input rows, dispatch to the type's handler,
validate the output, write `outputs/<stage>.parquet`, save the run's manifest — a typed
`WorkflowRun` record (`app.core.models.records.workflow_run`; `stages` is a list of typed
`StageRun` records) — to the document store's "workflow_run" collection.
- **Duplicate-input throw (every stage type):** fails the stage if any input dataframe has
  exact duplicate full-content rows — the error names the input id + 0-based row numbers.
  Identity is a content hash over the whole row; `primary_key` plays no part (optional, may
  legitimately duplicate). If N draws per row are intended, add an explicit row_id upstream.
- **Incremental manifest:** persisted after every stage (`running` → terminal) via
  `ctx["persist_manifest"]` — injected by the caller, so the UI shows live progress for a
  project run (`prepare_run`/`run_prepared`, saved to the store) while an ephemeral eval/
  preview subset run (`run_subset`) stays in-memory only.
- **Row slicing:** any stage may carry `limit: N` (throttle the LLM fan-out for a dry run).
  Per run, `--limit <id>=<N>` overrides it and `--offset <id>=<M>` drops the first M rows
  first (offset 5 + limit 3 = rows 6-8); recorded in the manifest, re-applied on resume,
  unknown ids fail loudly.
- **Halt + resume:** `human_review_queue` raises `HaltForReview`; the run marks
  `awaiting_review` and persists the pending queue. `resume_run(...)` reloads completed
  outputs and continues once decisions exist.

## `stages/` — one module per stage type (`HANDLERS`)
`input_data` connector `file` (csv/parquet/json/geojson; `_read_geojson` flattens a
FeatureCollection); `python_row_function`/`python_frame_function`
(`function: {kind: module|inline}`, row variant mapped per row); `join`; `aggregate`;
`llm_transform` (row-mapped, bounded parallelism);
`human_review_queue` (content-hash → prior decisions or halt);
`publish` (a `function` module that writes artifacts).

## LLM backend (`llm_transform`)
- `options.py` `require_agent_backend()` raises unless the agent backend can run
  (`claude_agent_sdk` importable and a `claude` CLI located, incl. Windows
  `~/.local/bin/claude.exe`). The agent is the ONLY backend — no fallback of any kind.
- `llm.py` `call_llm` renders the stage's prompt and runs a headless structured-output
  `app.core.agent.agent.Agent` whose `target_schema` is the stage's compiled reply model, so
  the reply is validated by construction rather than parsed from prose. A stage declaring
  `llm.tools` fails loudly — the agent backend doesn't support tools. Run per row by the
  row driver under bounded parallelism.

`validation.py` — DATA validation of a dataframe against an `output_schema` (columns, types,
ranges, nullability, PK uniqueness), distinct from the stage schemas in `app/core/models/`.

## Run / debug
```
python -m app.runtime.runner <project_dir>
```
Outputs: `runs/<id>/{outputs/*.parquet, artifacts/, queue/}` on disk; the run's
manifest is a document in the store (`run` collection, id `<project>/<run_id>`).
