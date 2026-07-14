# app/runtime — the Runner (workflow executor)

Executes a workflow and persists the result. Does not import the compiler or
the web app. Loads stages through `app/services/loader.py: load_workflow`,
which parses each compiled stage specification into a `Stage` object and raises
`WorkflowLoadError` if any stage or cross-stage check fails — an invalid workflow is
refused before the runner does any work.

## Files
- **`runner.py`** — the executor.
  - `topological_sort` → `execute_run(project_dir, repo_root)` runs the workflow once.
  - Per stage: validate declared inputs (`validation.py`), reject duplicate input
    rows (below), dispatch to the type's handler, validate the output, write
    `outputs/<stage>.parquet`, append a record to `manifest.json`.
  - **Duplicate-input throw (every stage type):** before dispatching the handler,
    the runner fails the stage if any input dataframe contains exact duplicate
    full-content rows — the error names the input id and the 0-based duplicate
    row numbers. Identity is a content hash over the whole row; the declared
    `primary_key` plays no part (it is optional and may legitimately duplicate).
    Rationale: duplicates at a stage boundary are ambiguous intent — an upstream
    bug, or sampling smuggled in implicitly. If N draws per row are intended, the
    author adds an explicit row_id/draw_id column upstream, making rows distinct.
  - **Incremental manifest:** the manifest is flushed after every stage (status
    `running` → terminal), so the web UI can show live progress and a run can be
    executed in a background thread. `prepare_run` (writes the initial manifest +
    returns the run context) / `run_prepared` support that background path.
  - **Row slicing (`limit:` + per-run `--limit`/`--offset`):** any stage may carry
    a top-level `limit: N`; the runner truncates that stage's output to the first
    N rows (used to throttle the expensive LLM fan-out for a dry run). Per RUN,
    `--limit <stage_id>=<N>` overrides the static cap and `--offset <stage_id>=<M>`
    drops the first M rows before the cap applies (offset 5 + limit 3 = rows 6-8) —
    both also as `limits=`/`offsets=` kwargs on `prepare_run`/`execute_run`. The
    overrides are recorded in the manifest (`limit_overrides`/`offset_overrides`),
    re-applied on resume, and unknown stage ids fail loudly.
  - **Halt + resume:** a `human_review_queue` handler raises `HaltForReview`; the
    runner stops, marks the run `awaiting_review`, and persists the pending queue.
    `resume_run(project_dir, run_id, repo_root)` reloads completed outputs and
    continues once decisions exist.
- **`stages/`** — one handler per node type (`HANDLERS` dict), one module per stage type.
  - `input_data` connectors: `file` (csv/parquet/json/**geojson**), `computed_static`
    (only kinds the `ConnectorKind` schema allows — use a committed snapshot via
    `file` for anything else). `_read_geojson` flattens a FeatureCollection.
  - `python_row_function` / `python_frame_function` (`function: {kind: module|inline}`;
    the row variant is mapped per input row by the runtime), `join`, `aggregate`,
    `llm_transform` (batched LLM calls), `human_review_queue` (filter → content-hash
    on `hash_columns`/upstream PK → apply prior decisions or `HaltForReview`),
    `publish` (runs a `function` module that writes artifacts).
- **LLM backends** (used by `llm_transform`):
  - `options.py` — config knobs (`CLAUDE_BIN`, `DEFAULT_MODEL`/`PARALLEL`/`TIMEOUT_S`)
    and `get_llm_call_type()`, which picks `agent_sdk | cli | mock` from
    `CW_LLM_BACKEND` (default `auto`: agent_sdk → cli). The mock is opt-in
    (`CW_LLM_FORCE_MOCK=1`); with no live backend it raises rather than silently
    mocking.
  - `llm.py` — `call_llm` / `call_llm_batch` render the prompt and dispatch; the
    JSON parser recovers the last JSON value embedded in prose (for tool-using
    research output). A stage's `llm.tools:` list (e.g. `[WebSearch, WebFetch]`)
    is honored only by the agent_sdk backend.
  - `llm_agent_sdk.py` — drives `claude_agent_sdk.query()`. Locates `claude` (incl.
    Windows `~/.local/bin/claude.exe`, which the SDK's own search misses). One
    tool-less query per row, no system prompt of our own; with `tools` it allows
    just those so the agent can web-research. `run_query` returns `{text, events}`
    (events = thinking/tool_use/tool_result/text) for tracing.
  - `llm.py` `call_llm_real` — the legacy `claude -p` subprocess path (cli backend).
  - `llm_mock.py` — deterministic offline mock (incl. an honest palm Tier-2 stub
    that never asserts a feature or invents a URL).
- **`validation.py`** — DATA validation of a dataframe against an `output_schema`
  (present columns, types, ranges, nullability, PK uniqueness). Distinct from the
  stage schemas in `app/models/`.
- **`lineage.py` + `trace.py`** — row-level lineage for row-*reshaping* stages
  (join fan-out, aggregate fan-in, human_review_queue drop/reorder, recoverable
  python_frame_function). Those handlers record `out_row, in_stage, in_row` edges
  into `ctx["lineage"]`; the runner re-slices them through the SAME offset/limit
  as the output and persists `runs/<id>/lineage/<stage>.parquet` (an opaque frame
  function that can't be recovered writes an `.untracked` marker instead). Row-
  *preserving* stages record nothing — output row *i* is input row *i* by
  position. `trace.py` walks (stage, out_row) back to origin rows, consuming
  sidecars where present and following the ordinal positionally where not.

## Run / debug
```
python -m app.runtime.runner examples/<name>          # auto backend
CW_LLM_FORCE_MOCK=1 python -m app.runtime.runner ...  # fast, deterministic, no LLM
CW_LLM_BACKEND=agent_sdk python -m app.runtime.runner # real model (needs claude CLI)
```
Outputs: `examples/<name>/runs/<id>/{manifest.json, outputs/*.parquet, lineage/*.parquet, artifacts/, queue/}`.
