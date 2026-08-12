# Run & review UI

The screens a workflow *operator* (vs. its author) uses: watching a run,
reviewing flagged rows, and versioning the workflow itself. Code:
`app/web/routers/{runs,run_lineage,review,node}.py` + `app/templates/`
(`run_detail.html`, `_run_stage_panel.html`, `queue.html`, `_node_panel.html`,
`versions.html`) + `app/static/{run-status,run-page,node-review,review-queue}.css`.
All routes live under `/project/{project}/…`.

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

### The stage panel (`_run_stage_panel.html`) — one strip of tabs

Loaded into the page via `innerHTML`. **Gotcha that bit us:** `innerHTML` does
NOT execute injected `<script>` tags, so `loadStage` re-creates script nodes
after injection — without that, the panel's JS (the tab strip, the run log) is dead.

- **Data** | **Schema** | **Transform**, opening on Data. Output and input sit
  in one pane, because the output reads as a diff *against* its input.
- **Data**: the stage's output (the stage-aware diff below, or the plain
  preview), validation, then the upstream input previews in an `input rows`
  disclosure, read-only. **Schema**: what each input supplies, then the stage's
  output — both resolved for the whole workflow and handed to the page as a
  `WorkflowStage`.
- The **simulator** is its own page, `…/stage/{sid}/simulate`, linked from
  Transform: the folded transform, the input rows with per-row checkboxes, the
  controls, then the result. Running it POSTs `…/stage/{sid}/preview`, which
  executes the handler in memory (real LLM calls for `llm_transform`) and
  persists nothing.
- **Full-table view + CSV**: `…/stage/{sid}/rows` renders the entire stage
  output (not just the first-5 preview) — as the same diff where one exists,
  over `MAX_TABLE_ROWS` rows instead of the panel's five, keeping the page's row
  numbers and click-to-expand cells; `?raw=1` serves the plain table instead,
  and each view names itself and links the other. `…/rows.csv` downloads the
  output uncapped, UTF-8 behind a byte-order mark so accented rows open
  correctly in Excel on Windows (`loading.csv_download_body`).
- **Stage-aware diff** (`app.web.stage_diff` → `_stage_diff.html`, in Data and
  on the full-rows page):
  a 1:1 stage (`python_row_function`, `llm_transform`, and `enrich` against its
  subject input) draws its INPUT frame as the base with what it did painted
  over. Every input column holds its input position: one the stage dropped is
  struck through, carrying the input value it discarded; the columns the stage
  added follow, tinted. Each column header carries the colour-free mark for what
  happened to it — `+` on an added column, `−` on a dropped one, where the strike
  takes the name and leaves the mark readable — and changed cells carry the
  replaced value struck through. A `filter_rows` stage renders ONE merged table
  over the first input rows in input order: kept rows exactly as the plain
  preview draws them (lineage links included), dropped rows in place, tinted
  and labelled — read off the stage's lineage sidecar, never guessed — noting
  drops beyond the shown window. The diff header is one horizontal axis, laid out
  the same way for either shape: the input frames stacked vertically, a bracket
  gathering them where there is more than one, a rail, then the output frame —
  a sibling of the input stack, so a second input lengthens the stack without
  moving the output. Each unit names its part in words — `base input` /
  `reference input` / `output`, so the base reads without colour — carries the
  row count of the frame it names, and links that frame's raw full-rows view
  (`?raw=1`) and CSV download; an `enrich`'s reference frame is a unit like any
  other, shown with no count wherever it could not be read. The rail carries one
  tally in one vocabulary, whichever shape produced it: the things the stage did
  that its own shape actually measured (`+2 cols · −3 cols · 0 cells changed` for
  a positional diff, `−121 rows` for a filter). A filter compares no cells and no
  columns, so it reports neither — a zero it never counted would be invented.
  Every other stage type keeps the plain output
  view, and any stage whose alignment can't be verified (missing frame,
  row-count mismatch, absent sidecar) falls back to it.

## Review queue (`queue.html`, `_queue_card.html`)

`GET /project/{p}/runs/{run_id}/queue/{stage_id}` renders the page;
`GET …/queue/{stage_id}/card/{input_fingerprint}` re-renders one card
(`_queue_card.html`, the same partial the page loops over) and 404s on a
fingerprint this queue does not carry.

A `human_review_queue` can follow **any** stage type, so the page assumes
nothing about the upstream stage or its column names. The queued row itself is
the material to review, split in two: the columns the queue declares as
`reviewed_columns` sources appear only in the review section, beside their
controls; every other column of the row is background context, rendered as a
key/value table (none of them under review → no table at all). `queue_page`
describes each column from what the queue stage's upstream supplies —
its `description` becomes the label's tooltip, and a column in the declared
`primary_key` carries a `key` flag. A `primary_key` is optional, so where one is missing the page
states that rather than guessing which columns identify a row. Each card's header
states its **position in the queue** (`Row 1 of 3`) — an opaque key identifies
nothing to a human, and the key column is already in the table, flagged.

**Review order** is authored, not chosen here: the page shows the snapshot's rows
in the order they are stored, and `queue.sort` (a list of column/direction keys on
the stage) is applied by the runtime, which permutes the snapshot, its
`input_fingerprints` and its `row_ordinals` together before writing them. So
`Row 1` is the first row the stage says to review, and no view code sorts
anything. An empty `queue.sort` leaves the upstream order. `sort` is an
INCIDENTAL config field, so re-ordering a queue does not change the stage
fingerprint and decisions already recorded still match.

**Paging** (`app/static/queue-paginate.js`, `_queue_pager.html`) is entirely on
the client — one route, one response, no reload. The server renders every card,
but inside a `<template>`, whose content is parsed and never laid out;
`createQueuePager` holds those cards and attaches `QUEUE_PAGE_SIZE` (25) of them
at a time to `#queue-items`. So layout costs a page, not a queue — including the
re-layout when one card grows, which is what recording a decision does. It bounds
LAYOUT ONLY: the whole queue is still transferred and parsed, at roughly 5.5 KB
of HTML per card, and nothing here compresses that.

The pager, not the live list, is a card's current state: `replaceCard` swaps the
re-rendered card into it as well as into the page, so paging away and back shows
a decision rather than the card the page loaded with. Everything else stays
whole-queue: `Row N of TOTAL` is server-rendered and absolute, the progress
count is seeded from `page.reviewed_count` rather than counted off the cards, and
the Prev/Next controls appear only when the queue runs past one page and only
once the reviewer gate is open.

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
was pre-filled with, and `queue_decide` RECORDS `modify` when any submitted
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
that follows settles its verdict against the recorded value the card opens on.
Decisions are keyed by a hash of the
row (`app.core.stage_cache`)
so they survive re-runs and LLM non-determinism. Recording a decision fetches
that row's card partial and swaps it in place. The recorded block a decided card
gains is rendered BELOW `.decision-controls`, so everything the swap adds falls
under the button just pressed and nothing above it moves — which is what lets
the swap happen under the reviewer without moving their place in a long queue.
The page counts the decisions it has recorded on top of
`page.reviewed_count` to move the progress bar, and reveals the **Resume run**
button (rendered `hidden`) once that count reaches the total.

## The node panel + workflow versioning (`_node_panel.html`, `versions.html`)

Reading and editing the *workflow itself*, stage by stage — distinct from
reviewing a run's flagged rows.

- `GET …/node/{stage_id}/panel` renders one stage's Inputs / Transform / Outputs
  / Spec; `GET /project/{p}/workflow/graph` re-renders the mermaid graph after an
  edit changes a stage's inputs.
- `POST …/node/{stage_id}/edit` is the **only** code path that writes to
  `compiled/`.
- `POST /project/{p}/version` freezes `compiled/` into a `Version` document (in
  the store); `GET /project/{p}/versions` lists the frozen versions.

## Where to confirm visually

Some states only render during a live run (spinner, yellow in-progress
borders). A halted/`awaiting_review` run exercises the progress framing, pending
borders, the alert strip, and the queue's model-input recovery. An errored run
exercises the re-run button.
