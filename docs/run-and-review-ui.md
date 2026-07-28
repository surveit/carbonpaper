# Run & review UI

The screens a workflow *operator* (vs. its author) uses: watching a run,
reviewing flagged rows, and approving/versioning the workflow itself. Code:
`app/web/routers/{runs,review,node_review}.py` + `app/templates/`
(`run_detail.html`, `_run_stage_panel.html`, `queue.html`, `_node_review.html`,
`versions.html`) + `app/static/style.css`. All routes live under
`/project/{project}/…`.

## Run detail page (`run_detail.html`)

`GET /project/{p}/runs/{run_id}`.

- **Progress framing** (not error-counting): the header shows *complete /
  in-progress / to-do* with a bar, a spinner while active, and an overall status.
  Warnings/errors/awaiting live in a **separate alert strip** that only appears
  when there's something to report.
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
  output (not just the first-5 preview); `…/rows.csv` downloads it uncapped.

## Review queue (`queue.html`)

`GET /project/{p}/runs/{run_id}/queue/{stage_id}`.

A `human_review_queue` can follow **any** stage type, so the page assumes
nothing about the upstream stage or its column names. The queued row itself is
the material to review, split in two: the columns the queue declares as
`reviewed_columns` sources appear only in the review section, beside their
controls; every other column of the row is background context, rendered as a
key/value table (none of them under review → no table at all). `queue_page`
describes each column from the schema the queue stage's input edge declares —
its `description` becomes the label's tooltip, and a column in the declared
`primary_key` carries a `key` flag. Where a declaration is missing the page says
so: an edge with no schema falls back to the queued rows' own columns with no
descriptions, and a stage with no declared `primary_key` states that rather than
guessing which columns identify a row. Each card's header states its **position
in the queue** (`Row 1 of 3`) — an opaque key identifies nothing to a human, and
the key column is already in the table, flagged.

**Lineage**: each card links to
`…/stage/{upstream_stage_id}/row/{row_ordinal}/trace/view`, where the ordinal
comes from the halted-queue sidecar's `row_ordinals` (written by the runtime,
positionally aligned to the snapshot). The queue stage has produced no output at
halt time, so it is the UPSTREAM stage's row that is traced. No ordinals, no
declared input, or more than one declared input → no link and a stated reason,
never a guessed one.

The page opens on a **"Reviewing as"** name field and the queue stays hidden
until a name is typed (remembered in `localStorage`); `queue_decide` rejects a
blank one with a 400, so no decision is recorded unattributed. The name is
written into `queue.reviewer_column` on every decision, alongside a timestamp in
`queue.reviewed_at_column`.

The form fields come from the stage's own `queue.reviewed_columns`: one control
per reviewed column, typed from that column's declared schema. The reviewer
names no verdict: the page posts both the values it submits and the values it
was pre-filled with, and `queue_decide` DERIVES `modify` when any submitted
value differs from the prefill the page carried, `approve` when they all match.
(`skipped` is the runtime's own verdict for a row its filter excluded; the
review service refuses it from a reviewer.) A decision records that verdict, a
value for each reviewed column, and optionally a note — it never overwrites the
column it reviewed, because a review stage may only ADD columns
(`app.models.stages.human_review_queue._find_added_column_collisions` rejects a
target that reuses an input column's name). Once a decision is recorded the card
stops asking for input: its per-field `change` openers carry the `disabled`
attribute and the primary **Submit** is replaced by a secondary **Change my
review**, which records nothing and only re-enables the controls; the re-submit
that follows derives its verdict against the recorded value the card opens on.
Decisions are keyed by a hash of the
row (`app.core.stage_cache`)
so they survive re-runs and LLM non-determinism. When all items are decided, a
**Resume run** button appears.

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
