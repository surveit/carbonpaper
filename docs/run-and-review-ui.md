# Run & review UI

The screens a workflow *operator* (vs. its author) uses: watching a run,
reviewing flagged rows, and approving/versioning the workflow itself. Code:
`app/web/routers/{runs,run_lineage,review,node_review}.py` + `app/templates/`
(`run_detail.html`, `_run_stage_panel.html`, `queue.html`, `_node_review.html`,
`versions.html`) + `app/static/style.css`. All routes live under
`/project/{project}/…`.

## Run detail page (`run_detail.html`)

`GET /project/{p}/runs/{run_id}`.

- **The header** (`app/web/run_header.py` → `_run_header.html`) is three things: a
  grounding line (start, duration, run id, pinned version + its message), one
  CTA chosen by run state, and the **stage strip** — one square per stage in
  topological order, coloured by status, with labelled counts beneath. The run's
  status is never spelled out in words; you read it off the CTA. The squares
  divide the header's width so every stage is drawn whatever the stage count.
- **The workflow graph is the main object**: full width, on top; the stage
  detail panel sits below it. Per-state borders via `build_mermaid_graph` status
  strokes: green=complete, yellow=in-progress, red=error, grey=pending,
  blue=awaiting review.
- **Live polling**: while the manifest says `running`, JS polls
  `GET …/runs/{id}/status` every 2s and updates progress + re-renders the graph
  in place, then reloads once on the terminal transition. Terminal runs don't
  poll.
- **Re-run failed stages**: on an errored run a button POSTs to `/resume`.
  Resume re-runs any non-complete stage (error + downstream) and **reuses
  completed upstream outputs** — no re-running finished stages, no new LLM calls
  for them. (The same mechanism powers "continue after review".)

### The stage panel (`_run_stage_panel.html`) — two layers of tabs

Loaded into the page via `innerHTML`. **Gotcha that bit us:** `innerHTML` does
NOT execute injected `<script>` tags, so `loadStage` re-creates script nodes
after injection — without that, the panel's JS (tabs + scratch tool) is dead.

- L1: **Schema** (the static spec) | **Current run** (this run's data).
- L2: **Inputs** | **Transform** | **Outputs**. 6 panes = L1 × L2.
- The **scratch tool** (in-memory re-run on picked rows; real LLM calls for
  `llm_transform`) lives in Inputs (row picker) and shows its result in
  Transform. Nothing is persisted.
- **Full-table view + CSV**: `…/stage/{sid}/rows` renders the entire stage
  output (not just the first-5 preview); `…/rows.csv` downloads it uncapped,
  UTF-8 behind a byte-order mark so accented rows open correctly in Excel on
  Windows (`loading.csv_download_body`).
- **Stage-aware diff in Outputs** (`app.web.stage_diff` → `_stage_diff.html`):
  a 1:1 stage's (`python_row_function`, `llm_transform`) output preview is a
  positional diff against its input — columns the stage added are tinted and
  named in words, changed cells carry the replaced value struck through, and
  the summary line counts changes over the whole frame. A `filter_rows` stage
  shows which input rows it dropped (read off its lineage sidecar, never
  guessed) above the normal preview. Every other stage type keeps the plain
  pane, and any stage whose alignment can't be verified (missing frame,
  row-count mismatch, absent sidecar) falls back to the plain pane.

## Review queue (`queue.html`)

`GET /project/{p}/runs/{run_id}/queue/{stage_id}`.

The key principle: **a reviewer must see the model INPUT, not just its output.**
The queue snapshot only holds the scoring stage's *output* (score + reasoning +
ids); the thing the model judged lives one stage upstream. `queue_page` walks
back from the queue stage → the scoring `llm_transform` stage → that stage's
input, loads the input's run output, joins each flagged row by primary key, and
**re-renders the actual prompt**. The card leads with *the scored text + the
exact prompt the model received*, then the model's output. If the input can't be
recovered it says so loudly ("reviewing blind") rather than hiding it. (Known
gap: when no primary key is declared, the join falls back to guessed keys —
issue #49.)

Decisions are content-hashed (`decisions/<stage>.parquet`) so they survive
re-runs and LLM non-determinism. When all items are decided, a **Resume run**
button appears.

## Node review + workflow versioning (`_node_review.html`, `versions.html`)

Reviewing the *workflow itself*, stage by stage — distinct from reviewing a
run's flagged rows.

- Each stage carries an approval state (`GET /project/{p}/review/status`,
  `POST …/node/{stage_id}/decide`). A decision is hashed over the stage's
  **loaded content** (minus loader bookkeeping keys), so editing a node
  invalidates its approval and approvals survive file reordering. The invariant
  and its trap live in `app/services/node_review.py`'s docstring — read it
  before touching the loader.
- `POST …/node/{stage_id}/edit` is the **only** code path that writes to
  `compiled/`.
- `POST /project/{p}/version` freezes `compiled/` into a `Version` document (in
  the store) with approval coverage recorded; `GET /project/{p}/versions` lists
  the frozen versions.

## Where to confirm visually

Some states only render during a live run (spinner, yellow in-progress
borders). A halted/`awaiting_review` run exercises the progress framing, pending
borders, the alert strip, and the queue's model-input recovery. An errored run
exercises the re-run button.
