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
- **Recompute everything:** `--bust-cache` (a run-form checkbox too) sets
  `RunContext.bust_cache`: the run skips every stage-cache READ while still recording
  what it computes, so the cache ends re-pinned, not stale. Recorded in the manifest
  and replayed on resume. A `human_review_queue` under it replays no decision — every
  queueable row halts again.
- **Halt + resume:** `human_review_queue` raises `HaltForReview`; the run marks
  `awaiting_review` and persists the pending queue. `resume_run(...)` reloads completed
  outputs and continues once cached decisions exist for the pending rows.

## `stages/` — one module per stage type (`HANDLERS`)
`input_data` connector `file` (csv/parquet/json/geojson; `_read_geojson` flattens a
FeatureCollection); `python_row_function`/`python_frame_function`
(`function: {kind: module|inline}`, row variant mapped per row); `join`; `aggregate`;
`llm_transform` (row-mapped, bounded parallelism);
`human_review_queue` (row fingerprint → cached decision or halt);
`publish` (a `function` module that writes artifacts).

**Row caching is a property of the handler SHAPE, not of a stage type.** `RowMapHandler`
wraps the one line of per-row compute (`execution._open_row_caching`), so `python_row_function`
and a batch_size-1 `llm_transform` are cached by the same code; for the batched path the
shape looks every row up, hands `run_llm_batches` only the misses, scatters the computed
rows back into input order alongside the hits, and records them. No stage module resolves a
cache. The store is `app.core.stage_cache` — `find_recorded_rows` is one bulk read per
execution, keyed by (stage-definition fingerprint, input-row fingerprint), and `record`
needs the write-capable `StageCache` accessor; the runtime holds that execution's state and
decides only whether caching applies and whether a result may be recorded. A row carrying
`_error`/`_deferred` is never recorded and no marker column is ever part of a recorded row,
so a hit reports no spend. `Stage.cache: false` declares a stage
intentionally non-deterministic — no read, no write — and is outside the definition
fingerprint. There is no per-registration opt-out: `human_review_queue` runs under the same
interceptor, which replays a human's recorded decision before its mapper is called, so that
mapper only ever passes a row through or defers it.

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
ranges, nullability, PK uniqueness), distinct from the stage schemas in `app/models/`.

## Run / debug
```
python -m app.runtime.runner <project_dir>
```
Outputs: `runs/<id>/{manifest.json, outputs/*.parquet, artifacts/, queue/}`.
