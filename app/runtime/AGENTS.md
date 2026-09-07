# app/runtime — the Runner (workflow executor)

Executes a workflow and persists the result. Does not import the compiler or the web app.
**It reads no workflow versions.** A caller resolves which version to pin and loads that
snapshot (`app/services/versioning.py`: `resolve_version_id` → `load_version_stages`) and
hands the runner that version as a `Workflow`; `app/services/run.py` is the one place that composes this for
a production run. An import-linter contract forbids `app/runtime/runner.py` from importing
`app.services` at all, so the arrow between them points one way only.

## `runner.py` — the executor
`topological_sort` → `execute_run(project_dir, repo_root, workflow, workflow_version)`. Per
stage: validate declared inputs (`validation.py`), dispatch to the type's handler, validate
the output, write `outputs/<stage>.parquet`, append to the run record.
- **Duplicate input rows are allowed.** A stage decides nothing per row-instance: the
  stage cache and a `human_review_queue` decision are both keyed on row CONTENT, so two
  identical rows share one cached result and one recorded decision — approving the content
  approves both. A reviewer is shown one card per row and cannot give the two different
  verdicts; the second posted verdict replaces the first.
- **Incremental manifest:** flushed after every stage (`running` → terminal), so the UI
  shows live progress and a run can execute in a background thread (`prepare_run`/`run_prepared`).
- **Row slicing caps what a stage READS, not what it emits:** it is a per-run parameter,
  never part of the workflow. `--limit <id>=<N>` cuts the window off each of that stage's
  INPUT frames before the handler is invoked — so an `llm_transform` under `--limit s=3`
  makes 3 calls, not 5,000 then a discard — and `--offset <id>=<M>` skips the
  first M rows first (offset 5 + limit 3 = upstream rows 6-8); a multi-input stage
  (`join`/`enrich`/`expand`, `union`) applies the same window to every input. The
  window is taken BEFORE the input-schema checks, so a limited run is not failed by a row
  it never reads. `input_data` is the one type with no input
  frames: its window is taken on the frame it just loaded, so a limit is never a
  silent no-op on a source. Recorded in the manifest, re-applied on resume, unknown
  ids fail loudly. A sliced stage's rows are recorded in its lineage sidecar under
  their TRUE upstream ordinals, so `trace` still lands on the right source row.
- **Recompute everything:** `--bust-cache` (a run-form checkbox too) sets
  `RunContext.bust_cache`: the run skips every stage-cache READ while still recording
  what it computes, so the cache ends re-pinned, not stale. Recorded in the manifest
  and replayed on resume. A `human_review_queue` under it replays no decision — every
  queueable row halts again.
- **Halt + resume:** `human_review_queue` returns an `AwaitingReview` on its `StageOutput`;
  the run marks `awaiting_review` and persists the pending queue. `resume_run(...)` reloads completed
  outputs and continues once cached decisions exist for the pending rows.

## `stages/` — one module per stage type (`HANDLERS`)
`input_data` connector `file` (csv/tsv/parquet/json/geojson; `_read_geojson` flattens a
FeatureCollection); `python_row_function`/`python_frame_function`
(`function: {kind: module|inline}`, row variant mapped per row);
`starlark_row_function` (`starlark_functions.py`, row-mapped; compiles the stage's
inline Starlark through `app/runtime/starlark_code.py`, the one place the interpreter
is driven and a `refuse(...)` call is translated to `StepRefused`); `enrich`/`expand`
(left join of inputs[1] into inputs[0]; `enrich` verifies m:1 and fails the run on a
non-unique reference, `expand` allows m:n fan-out); `aggregate`;
`llm_transform` (row-mapped, bounded parallelism);
`human_review_queue` (row fingerprint → cached decision or halt);
`report` (a `function` module that writes artifacts).

**A row-mapped stage sees only what its signature `reads`.**

**Row caching is a property of the handler SHAPE, not of a stage type.** There is one row
driver (`execution._run_row_mapper`): it narrows every row to the declared reads, answers
what it can from the cache, groups what is left, and records each group as it lands. The only
thing a stage type varies is `group_size` — one for every type but a batched `llm_transform`,
whose model call takes N — so every row-mapped type is keyed, recorded, logged, ordered and
rejoined by the same code. A group that completed therefore survives a later group's failure
with no batch-specific persistence anywhere. Hits are resolved before the grouping, so a
replayed row never takes a seat in a model call. No stage module resolves a cache. The store is `app.core.stage_cache` — `find_recorded_rows` is one bulk read per
execution, keyed by (stage-definition fingerprint, input-row fingerprint), and `record`
needs the write-capable `StageCache` accessor; the runtime holds that execution's state and
decides only whether caching applies and whether a result may be recorded. A row carrying
`_error`/`_deferred` is never recorded and no internal column is ever part of a recorded row,
so a hit reports no spend. `Stage.cache` decides whether a stage caches at all — no read,
no write when it is false — and is outside the definition fingerprint. It defaults to true
only on `llm_transform` and `human_review_queue`, the two types whose recompute spends a
model call or a human's attention; every other type recomputes unless its author turns
caching on. There is no per-registration row opt-out: `human_review_queue` runs under the same
interceptor, which replays a human's recorded decision before its mapper is called, so that
mapper only ever passes a row through or defers it.

## `run_log.py` — the per-run event log
`_execute_stages` opens a `RunLog` on the run's `run_events` chunks for every entry path and
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
  one GROUP of rows, over the input positions that group covers, so `llm.py` can log the
  prompt/thinking/response several frames down without a log being threaded through every
  mapper. The binding happens on the worker thread that makes the call — a pool thread starts
  with an empty context. Every emitter in `row_events.py` takes `RunLog | None` and no-ops on
  None, so the driver never branches on whether logging is on.
- **One function per group.** `_StageExecution.run_group` maps, validates, logs the outcome and
  records, in that order, as straight-line statements. It is deliberately not composed from
  wrappers: the order is the content, and nesting hides it.

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
  so `Stage.cache: false` — turning off the default this type carries — belongs on it unless
  the answer is genuinely expected to be stable.

`validation.py` — DATA validation of a dataframe against a stage's resolved output
schema (columns, types, enum vocabularies, ranges, nullability), distinct from the
stage schemas in `app/models/`. `report` is the one type that resolves none, and
the report says so rather than checking nothing silently. An error-severity issue in the OUTPUT report (missing column, failed coercion,
value outside a declared enum, null in a non-nullable column) fails the
stage: the record is `error` with an `OutputSchemaViolation` and downstream stages are blocked.
`validation_warnings` means warning-severity issues only. Input-side issues alone still only warn.
An out-of-`range` number is one of those errors, enforced at every level a number passes: the
reply an `llm_transform` may submit, the row its mapper returns, and the frame the stage lands.
A declared bound is a constraint, not a hint — do not declare one the data may legitimately
exceed. Inside an `llm_transform` the bound reaches the model as a `minimum`/`maximum` on the
submit tool, so a true value outside it cannot be reported: that pressure is the price of the
guarantee, and the reason a bound belongs only where it really holds.

`find_row_issues` runs the schema's own `to_pydantic_model` over each mapped row as its
mapper returns it, so a row off its signature fails AS that row. `range` reaches that model as
`ge`/`le`, nested `fields` included; key uniqueness still needs `validate_table`.

`key_coverage.py` — the one COVERAGE check, on `enrich` and `expand`. Everything in
`validation.py` asks whether the values present are allowed; this asks which key values
are missing, in both directions: a reference key no output row carries, and a subject key
the reference never lists. Its findings are appended to the stage's OUTPUT report as
warnings, so they reach the run issue index and the review packet by the same path every
other issue takes. Warning only — an absent key may be a real gap or an open universe,
and nothing in a workflow declares which.

## Run / debug
```
python -m app.cli <project>
```
`app/cli.py` is the CLI — a top-level entrypoint beside `app/main.py`, outside this
package: it drives `app/services/run.py` (which resolves the newest stored version and
loads its stages), never `runner.py` directly. `<project>` is a
NAME under the projects root, so a project outside it needs `CARBON_PAPER_PROJECTS_DIR`.
Outputs: the run record and its event chunks in the store, plus
`runs/<id>/{outputs/*.parquet, artifacts/, queue/}` on disk.
