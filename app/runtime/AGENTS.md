# app/runtime — the Runner (workflow executor)

Executes a workflow and persists the result. Does not import the compiler or the web app.
Loads stages through `app/services/loader.py: load_workflow`, which parses each compiled
stage spec into a `Stage` and raises `WorkflowLoadError` if any stage or cross-stage check
fails — an invalid workflow is refused before the runner does any work.

## `runner.py` — the executor
`topological_sort` → `execute_run(project_dir, repo_root)`. Per stage: validate declared
inputs (`validation.py`), reject duplicate input rows, dispatch to the type's handler,
validate the output, write `outputs/<stage>.parquet`, append to `manifest.json`.
- **Duplicate-input throw (every stage type):** fails the stage if any input dataframe has
  exact duplicate full-content rows — the error names the input id + 0-based row numbers.
  Identity is a content hash over the whole row; `primary_key` plays no part (optional, may
  legitimately duplicate). If N draws per row are intended, add an explicit row_id upstream.
- **Incremental manifest:** flushed after every stage (`running` → terminal), so the UI
  shows live progress and a run can execute in a background thread (`prepare_run`/`run_prepared`).
- **Row slicing:** any stage may carry `limit: N` (throttle the LLM fan-out for a dry run).
  Per run, `--limit <id>=<N>` overrides it and `--offset <id>=<M>` drops the first M rows
  first (offset 5 + limit 3 = rows 6-8); recorded in the manifest, re-applied on resume,
  unknown ids fail loudly.
- **Halt + resume:** `human_review_queue` raises `HaltForReview`; the run marks
  `awaiting_review` and persists the pending queue. `resume_run(...)` reloads completed
  outputs and continues once decisions exist.

## `stages/` — one module per stage type (`HANDLERS`)
`input_data` connectors `file` (csv/parquet/json/geojson; `_read_geojson` flattens a
FeatureCollection) + `computed_static`; `python_row_function`/`python_frame_function`
(`function: {kind: module|inline}`, row variant mapped per row); `join`; `aggregate`;
`llm_transform` (batched); `human_review_queue` (content-hash → prior decisions or halt);
`publish` (a `function` module that writes artifacts).

## LLM backends (`llm_transform`)
- `options.py` `get_llm_call_type()` picks `agent_sdk | cli | mock` from `CW_LLM_BACKEND`
  (default `auto`: agent_sdk → cli). Mock is opt-in (`CW_LLM_FORCE_MOCK=1`); with no live
  backend it raises rather than silently mocking.
- `llm.py` renders + dispatches (`call_llm`/`call_llm_batch`; the JSON parser recovers the
  last JSON value in prose). `llm_agent_sdk.py` drives `claude_agent_sdk.query()`, locates
  `claude` (incl. Windows `~/.local/bin/claude.exe`), and honors a stage's `llm.tools:`
  (e.g. `[WebSearch, WebFetch]`, agent_sdk only). `llm_mock.py` — deterministic offline mock.

`validation.py` — DATA validation of a dataframe against an `output_schema` (columns, types,
ranges, nullability, PK uniqueness), distinct from the stage schemas in `app/models/`.

## Run / debug
```
python -m app.runtime.runner examples/<name>          # auto backend
CW_LLM_FORCE_MOCK=1 python -m app.runtime.runner ...  # deterministic, no LLM
```
Outputs: `runs/<id>/{manifest.json, outputs/*.parquet, artifacts/, queue/}`.
