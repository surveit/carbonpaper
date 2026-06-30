# app/runtime — the Runner (DAG executor)

Executes a methodology DAG and persists the result. Reads stage dicts directly and
does not import the compiler or the web app. (It does not yet validate against the
`app/models.py` contract — that wiring is a TODO.)

## Files
- **`runner.py`** — the executor.
  - `topological_sort` → `execute_run(methodology_dir, repo_root)` runs the DAG once.
  - Per stage: validate declared inputs (`validation.py`), dispatch to the type's
    handler, validate the output, write `outputs/<stage>.parquet`, append a record
    to `manifest.json`.
  - **Incremental manifest:** the manifest is flushed after every stage (status
    `running` → terminal), so the web UI can show live progress and a run can be
    executed in a background thread. `prepare_run` (writes the initial manifest +
    returns the run context) / `run_prepared` support that background path.
  - **Generic `limit:`** — any stage may carry a top-level `limit: N`; the runner
    truncates that stage's output to the first N rows (used to throttle the
    expensive LLM fan-out for a dry run).
  - **Halt + resume:** a `human_review_queue` handler raises `HaltForReview`; the
    runner stops, marks the run `awaiting_review`, and persists the pending queue.
    `resume_run(methodology_dir, run_id, repo_root)` reloads completed outputs and
    continues once decisions exist.
- **`handlers.py`** — one handler per node type (`HANDLERS` dict).
  - `input_data` connectors: `file` (csv/parquet/json/**geojson**), `computed_static`;
    `http`/`scrape`/`api`/`sql`/`manual_upload` raise `NotImplementedError` (use a
    committed snapshot via `file` instead). `_read_geojson` flattens a FeatureCollection.
  - `python_transform` (`function: {kind: module|inline}`), `join`, `aggregate`,
    `llm_transform` (batched LLM calls), `human_review_queue` (filter → content-hash
    on `hash_columns`/upstream PK → apply prior decisions or `HaltForReview`),
    `publish` (runs a `function` module that writes artifacts).
- **LLM backends** (used by `llm_transform`):
  - `llm.py` — `resolve_backend()` picks `agent_sdk | cli | mock` from
    `CW_LLM_BACKEND` (default `auto`: agent_sdk → cli → mock) and `CW_LLM_FORCE_MOCK=1`.
    `call_llm` / `call_llm_batch` render the prompt and dispatch; the JSON parser
    recovers the last JSON value embedded in prose (for tool-using research output).
    A stage's `llm.tools:` list (e.g. `[WebSearch, WebFetch]`) is honored only by
    the agent_sdk backend.
  - `llm_agent_sdk.py` — drives `claude_agent_sdk.query()`. Locates `claude` (incl.
    Windows `~/.local/bin/claude.exe`, which the SDK's own search misses). Default:
    a tool-less single-turn JSON completion. With `tools`, it allows just those
    tools so the agent can web-research and cite real sources. `run_query` returns
    `{text, events}` (events = thinking/tool_use/tool_result/text) for tracing.
  - `llm.py` `call_llm_real` — the legacy `claude -p` subprocess path (cli backend).
  - `llm_mock.py` — deterministic offline mock (incl. an honest palm Tier-2 stub
    that never asserts a feature or invents a URL).
- **`validation.py`** — DATA validation of a dataframe against an `output_schema`
  (present columns, types, ranges, nullability, PK uniqueness). Distinct from the
  STAGE-SPEC contract in `app/models.py`.

## Run / debug
```
python -m app.runtime.runner examples/<name>          # auto backend
CW_LLM_FORCE_MOCK=1 python -m app.runtime.runner ...  # fast, deterministic, no LLM
CW_LLM_BACKEND=agent_sdk python -m app.runtime.runner # real model (needs claude CLI)
```
Outputs: `examples/<name>/runs/<id>/{manifest.json, outputs/*.parquet, artifacts/, queue/}`.
