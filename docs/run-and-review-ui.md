# Run & review UI

The two screens a methodology *operator* (vs. its author) uses: watching a run, and
reviewing flagged items. Both were recently reworked. Code: `app/main.py` routes +
`app/templates/run_detail.html`, `_run_stage_panel.html`, `queue.html`,
`app/static/style.css`.

## Run detail page (`run_detail.html`)

`GET /methodology/{m}/runs/{run_id}`.

- **Progress framing** (not error-counting): the header shows *complete /
  in-progress / to-do* with a bar, a spinner while active, and an overall status.
  Warnings/errors/awaiting live in a **separate alert strip** that only appears when
  there's something to report.
- **The DAG is the main object**: full width, on top. The stage detail panel sits
  below it, full width.
- **Per-state borders** (via `build_mermaid_graph` status strokes): green=complete,
  **yellow=in-progress (`running`)**, red=error, grey=pending, blue=awaiting review.
- **Live polling**: while `manifest.status == running`, JS polls the status JSON
  endpoint (`/runs/{id}/status`) every 2s and updates progress + re-renders the DAG
  in place (no full reload), then reloads once on terminal state. The status
  endpoint returns `counts` incl. `done/running/pending/awaiting`.
- **Re-run failed stages**: on an errored run a button POSTs to the existing
  `/resume` endpoint. Resume re-runs any non-complete stage (error + downstream) and
  **reuses completed upstream outputs** — no re-running finished stages, no new LLM
  calls for them. (Resume already powered "continue after review"; this just exposes
  it for errors.)

### The stage panel (`_run_stage_panel.html`) — two layers of tabs

Loaded into the page via `innerHTML`. **Gotcha that bit us:** `innerHTML` does NOT
execute injected `<script>` tags, so `loadStage` re-creates script nodes after
injection — without that, the panel's JS (tabs + scratch tool) is dead.

Tabs:
- L1: **Schema** (the static contract) | **Current run** (this run's data).
- L2: **Inputs** | **Transform** | **Outputs**.
- 6 panes = L1 × L2. Default: Current run › Outputs.
- The **scratch tool** (in-memory re-run on picked rows; real LLM calls) lives in
  Inputs (row picker) and shows its result in Transform.

## Review queue (`queue.html`)

`GET /methodology/{m}/runs/{run_id}/queue/{stage_id}`.

The key principle: **a reviewer must see the model INPUT, not just its output.** The
queue snapshot only holds the scoring stage's *output* (score + reasoning + ids);
the thing the model judged (the quote, the benchmark) lives one stage upstream.
`queue_page` therefore walks back from the queue stage → the scoring `llm_transform`
stage → that stage's input, loads the input's run output, joins each flagged row by
primary key, and **re-renders the actual prompt**. The card leads with *scored text
+ benchmark + the exact prompt the model received*, then the model's output. If the
input can't be recovered it says so loudly ("reviewing blind") rather than hiding it.

Decisions are content-hashed (`decisions/<stage>.parquet`) so they survive re-runs
and LLM non-determinism. When all items are decided, a **Resume run** button appears.

## Where to confirm visually

Some states only render during a live run (spinner, yellow in-progress borders).
A halted/`awaiting_review` run exercises the progress framing, pending borders, the
alert strip, and the queue model-input. An `errored` run exercises the re-run button.
