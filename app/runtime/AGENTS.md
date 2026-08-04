# app/runtime — the Runner (workflow executor)

Executes a workflow and persists the result. Does not import the compiler or the web app.
**It reads no workflow versions.** A caller resolves which version to pin and loads that
snapshot (`app/services/versioning.py`: `resolve_version_id` → `load_version_stages`) and
hands the runner the stages; `app/services/run.py` is the one place that composes this for
a production run. An import-linter contract forbids `app/runtime/runner.py` from importing
`app.services` at all, so the arrow between them points one way only.

## `runner.py` — the executor
`topological_sort` → `execute_run(project_dir, repo_root, stages, workflow_version)`. Per
stage: validate declared inputs (`validation.py`), reject duplicate input rows, dispatch to
the type's handler, validate the output, write `outputs/<stage>.parquet`, append to
`manifest.json`.
- **Duplicate-input throw (every stage type):** fails the stage if any input dataframe has
  exact duplicate full-content rows — the error names the input id + 0-based row numbers.
  Identity is a content hash over the whole row; `primary_key` plays no part (optional, may
  legitimately duplicate). If N draws per row are intended, add an explicit row_id upstream.
  The rule lives in `app/core/frame_checks.py` with the other cross-row rule (key
  uniqueness), so the stage-test suite validator applies both to a generated test's frames.
- **Incremental manifest:** flushed after every stage (`running` → terminal), so the UI
  shows live progress and a run can execute in a background thread (`prepare_run`/`run_prepared`).
- **Row slicing caps what a stage READS, not what it emits:** any stage may carry
  `limit: N`, and the window is cut off each of its INPUT frames before the handler is
  invoked — so an `llm_transform` with `limit: 3` makes 3 calls, not 5,000 then a
  discard. Per run, `--limit <id>=<N>` overrides it and `--offset <id>=<M>` skips the
  first M rows first (offset 5 + limit 3 = upstream rows 6-8); a multi-input stage
  (`join`/`enrich`/`expand`, `union`) applies the same window to every input. The
  window is taken BEFORE the duplicate-row and input-schema checks, so a limited run
  is not failed by a row it never reads. `input_data` is the one type with no input
  frames: its window is taken on the frame it just loaded, so `limit` is never a
  silent no-op on a source. Recorded in the manifest, re-applied on resume, unknown
  ids fail loudly. A sliced stage's rows are recorded in its lineage sidecar under
  their TRUE upstream ordinals, so `trace` still lands on the right source row.
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
(`function: {kind: module|inline}`, row variant mapped per row); `enrich`/`expand`
(left join of inputs[1] into inputs[0]; `enrich` verifies m:1 and fails the run on a
non-unique reference, `expand` allows m:n fan-out); `aggregate`;
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
`_error`/`_deferred` is never recorded and no internal column is ever part of a recorded row,
so a hit reports no spend. `Stage.cache: false` declares a stage
intentionally non-deterministic — no read, no write — and is outside the definition
fingerprint. There is no per-registration opt-out: `human_review_queue` runs under the same
interceptor, which replays a human's recorded decision before its mapper is called, so that
mapper only ever passes a row through or defers it.

## `run_log.py` — the per-run event log
`_execute_stages` opens a `RunLog` on `runs/<id>/events.jsonl` for every entry path and
closes it (writing the terminal `run_done` marker) before returning. Workers emit
lock-free; one writer thread stamps a monotonic `seq` + `ts` + `level` and appends one JSON
line per event. `read_events_since(path, from_seq)` re-reads it for both the run page's SSE
tail (`GET /project/{p}/runs/{id}/events`) and after-the-fact investigation. The manifest
stays the source of truth for stage status; this log is only ever the drill-down.
- **Two levels.** 0 = lifecycle (`run_start`, `stage_start`/`stage_done`,
  `row_start`/`row_ok`/`row_error`); 1 = LLM detail (`llm_prompt`, `llm_thinking`,
  `llm_text`, `llm_response`, `llm_tool_result`, `llm_error`), off by default on the run
  page and revealed by a client-side filter over the same feed. `llm_response` is the
  answer the model submitted; `llm_tool_result` is the verdict that came back on it, and
  is the only record of a call the tool layer rejected before the tool function ran.
- **Cached vs computed.** Every terminal row event carries `source`. A row the stage-result
  cache answered emits ONE `row_ok` marked `cached` — no `row_start`, no LLM detail,
  because nothing ran.
- **Detail attribution.** The row driver binds a `DetailSink` ContextVar for the duration of
  one row (the batched path binds one per chunk, over the input positions that chunk
  covers), so `llm.py` can log the prompt/thinking/response several frames down without a
  log being threaded through every mapper. The binding happens on the worker thread that
  makes the call — a pool thread starts with an empty context.

## LLM backend (`llm_transform`)
- `options.py` `require_agent_backend()` raises unless the agent backend can run
  (`claude_agent_sdk` importable and a `claude` CLI located, incl. Windows
  `~/.local/bin/claude.exe`). The agent is the ONLY backend — no fallback of any kind.
- `llm.py` `call_llm` renders the stage's prompt and runs a headless structured-output
  `app.core.agent.agent.Agent` whose `target_schema` is the stage's compiled reply model, so
  the reply is validated by construction rather than parsed from prose. Run per row by the
  row driver under bounded parallelism.
- **A stage declaring `llm.tools` researches.** The names (from
  `models.stages.llm_transform.GRANTABLE_TOOLS`) are granted to the agent alongside
  `submit_answer`, and the row moves onto the research budget — `RESEARCH_TIMEOUT_S` and
  `RESEARCH_MAX_TURNS` instead of `DEFAULT_TIMEOUT_S` and the submit-only turn cap — because
  searching and reading documents is the work, not overhead on top of it. Such a stage is NOT
  a pure function of its input row: re-running it may legitimately return a different answer,
  so `Stage.cache: false` belongs on it unless the answer is genuinely expected to be stable.

`validation.py` — DATA validation of a dataframe against an `output_schema` (columns, types,
enum vocabularies, ranges, nullability, PK uniqueness), distinct from the stage schemas in
`app/models/`. An error-severity issue in the OUTPUT report (missing column, failed coercion,
value outside a declared enum, null in a non-nullable column, duplicate primary key) fails the
stage: the record is `error` with an `OutputSchemaViolation` and downstream stages are blocked.
`validation_warnings` means warning-severity issues only. Input-side issues alone still only warn.
An out-of-`range` number is the deliberate exception, still a warning: a range bounds the
expected, an enum the possible.

## Run / debug
```
python -m app.runtime <project>
```
`__main__.py` is the CLI: it drives `app/services/run.py` (which resolves the newest
published version and loads its stages), never `runner.py` directly. `<project>` is a
NAME under the projects root, so a project outside it needs `CARBONPAPER_PROJECTS_DIR`.
Outputs: `runs/<id>/{manifest.json, events.jsonl, outputs/*.parquet, artifacts/, queue/}`.
