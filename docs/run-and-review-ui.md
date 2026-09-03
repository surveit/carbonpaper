# Run & review UI

The screens a workflow *operator* (vs. its author) uses: watching a run,
reviewing flagged rows, and versioning the workflow itself. Code:
`app/web/routers/{runs,run_lineage,review,node}.py` + `app/templates/`
(`run_detail.html`, `_run_stage_panel.html`, `queue.html`, `_node_panel.html`,
`versions.html`) + `app/static/{run-status,run-page,node-review,review-queue}.css`.
All routes live under `/project/{project}/…`.

## Runs index (`section_runs.html`)

`GET /project/{p}/runs`, and the same table at `?archived=1`.

- One row per stored run, newest first, drawn from `app.web.run_index`. A run
  whose record will not parse still gets a row, from its id alone.
- **Archiving** (`app/web/routers/run_archive.py` →
  `app/services/run_manifest_metadata.py`) moves a run between the two lists and
  does nothing else: a `run_manifest_metadata` record — what the operator records
  about a run, as against what the run recorded about itself — carries the flag,
  the run's own manifest is never touched, and the run page, its outputs and the
  spend it counts toward are unaffected. The runs picker lists the unarchived side.

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
- **Restart run**: the toolbar menu (☰) carries the same `/resume` POST whatever the
  run's status says, including `running` — an executor that dies mid-run leaves the
  status behind it, and cancel needs a live executor to consume it. Nothing checks
  whether the run is still executing: a second executor writes the same manifest and
  the same `outputs/<stage>.parquet`, last write wins. It goes inert once every stage
  has completed, since a resume would then run none of them
  (`run_header.describe_restart`).
- **Duplicate run**: the same menu links `/runs/new?from_run=<run_id>`, which opens
  the run form on that run's settings — version, per-row file and row cap, cache
  choice, and caps on stages with no row of their own as hidden fields
  (`run_inputs.build_run_input_choices`). It fills the form in and stops; the reader
  submits it. Changing the version drops the hidden caps, which name the copied
  version's stages.

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
  and each view names itself and links the other. Both orders their columns the
  way the diff does — what the stage wrote first — as do the panel's plain
  output preview and the review packet's stage page; an upstream input preview
  is drawn as its own producer wrote it, since nothing on it is this stage's
  work. `…/rows.csv` downloads the
  output uncapped, UTF-8 behind a byte-order mark so accented rows open
  correctly in Excel on Windows (`loading.csv_download_body`).
- **Stage-aware diff** (`app.web.stage_diff` → `_stage_diff.html`, in Data and
  on the full-rows page):
  a 1:1 stage (`python_row_function`, `llm_transform`, and `enrich` against its
  subject input) draws its INPUT frame as the base with what it did painted
  over. The columns its signature declares it rewrites or adds are drawn first,
  tinted, so the reason the reader opened the stage is not off the right edge
  behind a horizontal scroll (`app.web.column_order`, over
  `signature.list_written_column_names` — presentation order only, and the frame
  on disk and the CSV download keep the order the stage wrote). Behind them
  every input column holds its own relative position: one the stage dropped is
  struck through, carrying the input value it discarded. Each column header carries the colour-free mark for what
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
  row-count mismatch, no lineage recorded) falls back to it.

## Review queue (`queue.html`, `_queue_card.html`)

`GET /project/{p}/runs/{run_id}/queue/{stage_id}` renders the page;
`GET …/queue/{stage_id}/card/{input_fingerprint}` re-renders one card
(`_queue_card.html`, the same partial the page loops over) and 404s on a
fingerprint this queue does not carry.

A `human_review_queue` can follow **any** stage type, so the page assumes
nothing about the upstream stage or its column names. The queued row itself is
the material to review: the columns the queue declares as `reviewed_columns`
sources each render as their own row in the review section, one field decided
at a time. `queue.context_columns` optionally names the ordered background
context rendered as a key/value table. When omitted, every other column the
stage reads remains visible for compatibility; an empty list renders no context.
This display choice does not remove columns from the stage output. A context
column must exist in the input schema, be read by the stage, appear only once,
and not also be reviewed. `queue_page`
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

The form fields come from the stage's own `queue.reviewed_columns`: one row
per reviewed column, typed from that column's declared schema, each carrying a
`data-state` of `start`, `approved`, `modified`, `editing`, or `locked`. The
stylesheet renders each state by hiding what that state does not show; the
script moves the attribute and writes the value the row displays
(`showCurrentValue`), the control's value, the submit's `disabled`, the
readout beside it, and a visually-hidden `role="status"` note per field, since
a state that changes by `display` alone announces nothing.
`start` offers **Approve** and **Change**; `approved` and `modified` offer
**Change** and **Revert**; `editing` shows the control with **Save** and
**Cancel**, plus a hint that appears once the entered value matches the
prefill. Saving a value equal to the prefill lands the field in `approved`,
not `modified`. **Revert** returns a field to `start` and restores the
prefill; **Cancel** returns it to whichever state the editor was opened from.
The primary **Submit** stays `disabled` until every field is `approved` or
`modified`, and a readout beside it counts how many of the reviewed columns
are decided.

A field in `start` shows one value: the one it will submit if approved — on an
undecided row that is what the stage produced, on a decided row it is the
recorded value, and the row's received value is named beside it (`received:`)
while the card is unlocked. A `locked` field strikes the received value through
ahead of the recorded one ONLY where the two differ. The two are compared in the
view model (`ReviewItem.departs_from_received`), reading both in the spelling the
field's own control uses — a `date` control spells a received `2026-03-04T00:00:00`
as the `2026-03-04` a record of it holds, so approving that value strikes nothing.

The reviewer names no verdict: the page posts both the values it submits and
the values it was pre-filled with, and `queue_decide` RECORDS `modify` when
any submitted value differs from the prefill the page carried, `approve` when
they all match. (`skipped` is the runtime's own verdict for a row its filter
excluded; the review service refuses it from a reviewer.) A decision records
that verdict, a value for each reviewed column, and optionally a note — one
note for the row, from the single box under the fields, never one per field. It
never overwrites the column it reviewed, because a review stage may only ADD
columns (`app.models.stages.human_review_queue._find_added_column_collisions`
rejects a target that reuses an input column's name). Once a decision is
recorded the card locks: it offers no per-field controls, and the primary
**Submit** is replaced by a secondary **Change my review**, which returns
every field to `start` — so each must be decided again before Submit
re-enables — and records nothing itself. The submit that follows settles its
verdict against the SAME prefill the page carried, which on a decided row is
the previously recorded value: re-approving every field records `approve`
again, and changing any one of them records `modify`. `queue_decide` records each
decision twice: once into the stage cache, keyed by a hash of the row
(`app.core.stage_cache`) so re-runs and LLM non-determinism replay it, and once as an
append-only `ReviewDecision` row (`app.models.records.review_decision`, docs/run-manifest.md)
that no cache eviction can erase. Recording a decision fetches
that row's card partial and swaps it in place. The recorded block a decided card
gains is rendered BELOW `.decision-controls`, so everything the swap adds falls
under the button just pressed and nothing above it moves — which is what lets
the swap happen under the reviewer without moving their place in a long queue.
The page counts the decisions it has recorded on top of
`page.reviewed_count` to move the progress bar, and reveals the **Resume run**
button (rendered `hidden`) once that count reaches the total.

**A queue closes with its halt.** The snapshot and its fingerprints sidecar
outlive the halt — nothing deletes them, and a resume that finds every row
decided rewrites neither — so the page still renders after the run moves on.
What settles whether it takes decisions is the run's own record of THAT stage
(`find_review_closed_note`): `awaiting_review` and only that leaves it open.
Anything else fills `QueuePage.closed_note`, which says why in a sentence and is
what both templates read. A closed page draws the recorded value where the
control stood, no reviewer gate, no Resume, and no CTA; the script keeps the
pager and registers nothing that posts. `queue_decide` refuses with a 409 on the
same note, so the read-only page is a consequence of the refusal rather than a
substitute for it — a decision recorded after the resume would sit in the stage
cache contradicting the rows this run already emitted.

The way back in is the stage panel. There is one destination either way, so
there is one link: `find_queue_link` returns it wherever this run left a queue
snapshot and None elsewhere, and the stage's status picks the words — **Open
review queue** on a halted stage, **Read the review decisions** once the run has
moved on. The snapshot, not the stage type, is what the link is keyed on: a
queue stage whose filter excluded every row wrote none, and offering a page that
reads "no items to review" is a dead link.

## One file (`file_detail.html`, `_file_column.html`)

`/project/<id>/files/<file_id>`, which the Files table's rows open. Four sections under
a head naming the file: what its holder claims about it, its shape, its first rows, and
the runs that read it — then the delete, which takes the filename typed back.

**Data completeness** is a claim about the rows, not a state of the work. `closed` says
these rows are all of them, `sampled` says they are a subset and the note beside it says
how it was drawn (the save refuses a sampled file with an empty note), `open` says
nobody has claimed either way. It is not a run state and takes no run colour.

**Shape** is what the page exists for. Each column carries how much of it is filled,
how many distinct values it holds, and a glance at those values: a histogram for
numbers, a timeline for dates, the character range for prose, the commonest values for
a set. Opening one lists its values, with the empty string and the null ranked among
them — a column 96% blank says so in its first bar. Filled, blank and null are three
separate counts because they answer different questions: a Meltwater export arrives 100%
non-null with 17 of its 51 columns holding an empty string in every row, and a null
count alone calls all 51 full. The shares are over every row; a column that is not
wholly filled offers a switch to read them over the rows that carry a value instead.

`app/core/file_shape.py` measures a column from its values (no pandas);
`app/services/frame_profile.py` reads the file and hands it those values, and
`read_file_shape` keeps what it measured — a stored file's bytes never change, so the
Files index and this page read no files after the first look at each.

## Telling one file from another (`app/core/file_comparison.py`)

The Files index carries a **tells it apart** column. Nothing there decides what a column
MEANS. A column earns the cell by how much the files DISAGREE about it, and only ever
against files that carry the same columns — comparing a lexicon to a social export says
nothing, so the files are grouped by their exact column set first and a group of one is
told it is alone.

Disagreement is the mean pairwise total-variation distance between the files'
distributions over a column's commonest values, with everything else as one bucket. It
is a distance between distributions rather than an overlap between value sets, because
the case that matters is four Meltwater exports drawing on the SAME four queries in
different proportions: their value sets are identical and their mixtures are not.

Two guards keep it honest. A column whose listed values cover less than half a file's
rows is not compared at all — an id, a headline, a timestamp, where two files differing
means nothing. And of the columns that pass, the one shown is the one whose LEADING
values differ across the most files, not the one that disagrees most: `Author Name`
disagrees most across those four exports and every one of them leads with
`Comment on Valeurs actuelles`, which is a cell that reads the same on every row.
When no column clears the floor, the page says nothing separates them.

## The node panel + workflow versioning (`_node_panel.html`, `versions.html`)

Reading and editing the *workflow itself*, stage by stage — distinct from
reviewing a run's flagged rows.

- `GET …/node/{stage_id}/panel` renders one stage's Inputs / Transform / Outputs
  / Spec; `GET /project/{p}/workflow/graph` re-renders the mermaid graph after an
  edit changes a stage's inputs.
- `POST …/node/{stage_id}/edit` is the **only** code path that writes to the
  working copy.
- `POST /project/{p}/version` freezes the working copy into a `Version` document
  (in the store); `GET /project/{p}/versions` lists the frozen versions.

## The conversation, on every page (`_chat_panel.html`, `_chat_rail.html`)

The chat is a panel, not a page, and two hosts draw the same partial:

- `chat.html` at `/chat/{sid}` — the full-width page.
- `_chat_rail.html`, included from `base.html`, so every page but the review
  packet can hold one. Which session is open lives in `localStorage` and the
  panel arrives from `GET /chat/{sid}/panel`. No other route is handed any chat
  state.

The two things the rail remembers have different lifetimes on purpose. **Which**
conversation, and whether it is shut, are the reader's and go in `localStorage`;
**where** they had got to in it belongs to one view and goes in `sessionStorage`.
Per-tab for the first was tried and reverted: a tab opened from outside the
browser inherits no session storage, and links arrive that way constantly — from
another app, a bookmark, a restored window — so the rail was absent on most
arrivals, which is the one thing it exists not to be. A cold tab having no
reading place is not the same problem: the reader has not read anything in it
yet, so the newest turn is the right place to start.

Two scripts, and the split matters. `_chat_rail_head.html` runs inline in
`<head>`: it stamps `chat-rail-open` or `chat-rail-shut` on `<html>` so the page
**lays out with the column already reserved**, and it starts the panel fetch
there rather than after the foot scripts. Left to `DOMContentLoaded` both cost a
visible reflow — `main` from 1440 to 1040 and every mermaid diagram re-laying
out — which read as the rail taking seconds to arrive when the fetch itself is
about 3ms. `static/chat-rail.js` then fills the reserved column in.

`lineage.html` includes both scripts itself. It is standalone by necessity — the
same template is also written into a review packet as a file in a zip — so it
guards them on `offline`, which is what tells the two renders apart.

The rail does not survive a page load and does not need to. A turn is a detached
task with a replayable event buffer (`app/core/agent/turns.py`), so the panel
re-mounts on the next page and reattaches at `?from=0`.

The reader's place is kept in `sessionStorage` as **which message they were
reading and how far into it**, not a scroll offset: the page draws the transcript
at 1280px and the rail at 400, so the same offset is a different part of the
conversation. Both hosts store it against whichever box actually scrolls — the
log in the rail, the document on the page.

`static/chat-panel.js` is the client, scoped to a root element rather than the
document — every hook is a `js-` class, since an id would collide between two
panels. It makes two decisions about every link in a reply, both from one
question — is this link back into this app?

- **Where it opens.** `markdown_render` marks every link `target="_blank"`, which
  was right while the chat was a page you lost by clicking anything on it. The
  client drops it again for an in-app link: the rail carries the conversation
  across an in-app navigation, and a new tab is now the only thing that still
  loses it. Undone in the browser rather than at the renderer because origin is a
  fact about the browser, which a server behind a proxy does not have. An
  external link keeps its new tab.
- **How it is drawn.** An in-app link that is a whole paragraph on its own is a
  handover — the page the agent wants opened — and is drawn as a target
  (`.ac-goto`). A link inside a sentence is a citation and stays inline. Nothing
  is intercepted and no agent knows either decision happened.

Neither host names an agent. What a surface calls one is
`AgentConfig.display_name`, and `tests/arch/test_chat_rail_names_no_agent.py`
fails a build where a chat host learns an agent id.

## Where to confirm visually

Some states only render during a live run (spinner, yellow in-progress
borders). A halted/`awaiting_review` run exercises the progress framing, pending
borders, the alert strip, and the queue's model-input recovery. An errored run
exercises the re-run button.
