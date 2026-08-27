# app — the FastAPI web app (workflow / run / review UI)

`app/main.py` is a thin bootstrap: creates the FastAPI app, mounts `/static`, includes the
routers in `app/web/routers/`, which import the Runner (`app.runtime`) and the schemas
(`app.models`) and share `app/web/{config,loading,diagrams}`. Run:
`python -m uvicorn app.main:app --port 8765`.

Writing markup: read `docs/visual-language.md` first — colour, the agent mark, error vs
warning, and the arch test holding each. `app/templates/AGENTS.md` names the three that
bite most often.

## Pages / routes
- `/` project list · `/project/<m>` the project shell (Overview · Document · Terms ·
  Workflow · Runs); the Workflow section carries the mermaid graph + inline node review
  (`/project/<m>/node/<id>/review-partial`).
- `/project/<m>/runs`, `/runs/<id>` — run history + detail. `/runs/new` is the
  run-launch form (version picker, one path field + row cap per file input, and an
  **Advanced** fold holding the row-level cache checkbox) — the one surface where a run
  is configured, which the history page's ▶ New run, the Workflow page's ▶ Run
  workflow and a version page's ▶ Run this version all link to — the last through
  `?version_id=`, which pre-picks that version and binds its authored input paths (an
  id no stored version carries 404s rather than opening on the latest). Registered
  ahead of `/runs/<id>`, which would otherwise read `new` as a run id. The fold is closed on load and `<details>` submits its content
  either way, so the default run reuses cached rows without the reader deciding.
- `/project/<m>/runs/<id>/queue/<stage>` — the human-review queue UI (+ `/decide`, `/resume`).

## Zero states (`.empty-state`, styled in `app/static/split-view.css`)
A list, panel or section with nothing in it reads as a heading naming what is absent,
ONE line saying what would fill it, and a `.btn.primary` that does the filling — never a
link buried in the sentence, because the whole point of the screen is that there is one
thing to do next. Where the app offers no action for it (a workflow authored from an MCP
client, an eval form that does not exist yet), the heading and line stand alone; the
button is not invented to complete the shape.

## The run page's two columns (`run_detail.html`)
`.run-main` on the left, `.run-nav` (360px, collapsible) on the right. The nav column is the **spine** and holds
the **Run walkthrough** alone (the `ReviewGuide` record — the name is internal, the screen says
walkthrough) — so it is rendered **only** where the pinned version carries a guide, and the work
column takes the whole width otherwise (`.run-shell.no-nav`). It LEADS on the files the run
published (`.guide-published`), then the numbered steps: the result first, the account of how it
was reached under it. Writing a guide is offered on the version page, which is where one is stored. The work column is four named
sections, in this order:

1. **Run overview** — the header (grounding line, CTA, status bar) and the issue index.
   Everything about the run; nothing that is its result.
2. **Run outputs** — the files a publish stage wrote, off `header.artifacts`, as links
   with their sizes (`_run_outputs.html`, shared with the review packet's index).
   Absent when there are none. Never a button: a CTA is an imperative, a run that finished
   clean has none (`choose_run_cta` returns an empty `RunCta`), and a primary button sized
   to a filename was the widest thing on the page.
3. **Workflow** — the minimap.
4. **Stage details** — the stage panel. `hidden` until `loadStage()` unhides it: before a
   stage is picked there is nothing to show, and a heading over an empty box is exactly the
   kind of always-on furniture this page was cut down to remove.

The **toolbar** (`.run-toolbar`) shares Run overview's heading line (`.run-overview-head`,
heading left, actions right — neither spends a row of its own): the review packet, offered
twice — **Open** (`…/runs/<id>/packet`, the exported folder served page by page, so there is a
link to send someone who has downloaded nothing) and **Export** (the zip) — and beside them a
menu button holding the raw manifest and the whole-run log
(`.run-audit-menu`, closed on load, and closed again by Escape or a click outside it). The
heading line, not the toolbar, is the menu's positioning context, so the menu can be bounded
by the column's width instead of hanging off its left edge into `.run-main`'s clip.
Neither of those two is part of judging the run, so neither is on the page until the menu is
opened — which is also when the log's SSE feed connects, on a live run as on a finished one.
The export stays outside the menu: handing the packet to someone outside is a thing a reader
comes here to do.

## The issue index (`app.web.run_issues` → `_run_issues.html`)
Inside Run overview, under the header — and again on the **review packet's index**, its only
statement of what went wrong: ONE list indexing the stages, every entry one line plus a link,
and the stage panel names none of them itself. On the run page it stays in the
work column, not the nav rail: its four-column table needs the width. Drawn with
`_issue_table.html`, the panel + row macros the **Workflow** page's compiler warnings also use;
the macros own the heading (`17 warnings, 2 errors`, a severity with none of them left out) and
the CLOSED default, so neither page can word its counts differently or open on a different one.
The counts are the summary, so a closed panel still says something is wrong.
- **A stop** — an `error` stage, the run's own end — is the FIRST line, marked `stopped`, its
  message naming which failure it is, because they route to different people: a schema
  refusal (`OutputSchemaViolation`) says the data changed and links the panel's **Data** tab;
  an authored `StepRefused` reads `Input validation failed on <stage id>` and links its
  **Transform** tab; any other exception is the code's and keeps its type.
  What only a stop carries nests under its own line through the row macro's call block — the
  columns it refused, the reason its author wrote, its traceback, and the stages downstream
  that never ran, read off the pinned version's edges (with no readable version it names none
  rather than blaming the pending stages it can see).
- **Then every issue that did not stop it** (warnings anywhere, plus an error-severity INPUT
  issue, which only warns its stage), one line per stage × column × message, stages in the
  run's own order.

A deep link loads the panel through `_loadStage(id, {tab, reveal})`, which the panel serves by
publishing `_selectTab` on its root element. `reveal` smooth-scrolls the panel's top to the top of the
run column; every click the reader aims — the guide's steps and output links, an issue link, a
graph node — uses it, and a load they did not ask for (deep link, panel self-refresh) does not.

## The run page's workflow minimap (`.diagram-minimap` in `run_detail.html`)
The graph is a 200px band held at `data-zoom-floor` — `diagram_viewport.js` will not fit a
wide graph below that scale, so labels stay readable and the band is panned instead. Zoom,
fit and fullscreen are icon buttons overlaid in its top-right. It opens
parked on the run's first stage (`_focusNode(..., {select: false})` — scrolls without
outlining, so the band shows the flow's start without claiming a stage the reader has not
picked). Clicking a node loads the stage section, which until then is not on the page.

**⛶ is the SURVEY.** The `.diagram-block` goes fullscreen, not the viewport, so the whole
graph arrives with `.diagram-survey` under it: how many stages the run has, how many the
guide narrates, and how many it narrates nobody — every figure COUNTED off this run's stage
list and this version's guide. With no guide the narration lines are absent, not zeroed.

## The stage panel — three tabs (`run_stage_partial` → `_run_stage_panel.html`)
**Data │ Schema │ Transform**, one flat strip; it opens on Data:
- **Data** — what this run's stage produced: its output — rendered as a **diff against its
  input** where the stage type permits one (below) — then validation **as part of the
  output** (input + output issues from the manifest), then the upstream input previews,
  folded in an `input rows` disclosure — read-only, since picking rows to run on is its own
  page. URL cells are full clickable links. Compiler notes live on `/compile`, not here.
  The `stat-strip` (model · calls · cost) stays ABOVE the rows — those are facts about the
  run. A **caveat** on the rows (batched judging; an unreadable pinned definition) is a
  `.stage-caveat` `<details>`, closed, its whole warning in the summary line.
- **Schema** — the static contract: the input schemas, then the output schema.
- **Transform** — the *raw* transform config block (`_stage_executable.html`): llm prompt+model+tools,
  join keys, aggregate ops, connector/queue/publish spec — plus the only link to the
  simulate page below. An authored-code block (`function` / `filter`) reads **description → examples → code**:
  the block's plain-language `summary` leads, the test cases follow, and the source is rendered
  last and folded (`_stage_code.html`), because the reviewer is a journalist, not an engineer.
- **The diff** (`app.web.stage_diff` → `_stage_diff.html`): a 1:1 stage
  (`python_row_function`, `llm_transform`, `enrich` — against its subject input) reads as a
  positional diff over its INPUT frame as the base: the columns its signature declares it
  REWRITES or ADDS come first, tinted (`app.web.column_order` — the same order the plain output
  table and the packet's stage page use, so a column the reader came for is not behind a
  horizontal scroll), then the input frame's own columns in their own order, one the stage
  dropped struck through carrying the input value, changed
  cells marked; each header carries a `+` or `−` so both read without colour. A stage whose
  pinned version does not resolve declares nothing, and every surface keeps frame order.
  Presentation only: the frame on disk and the CSV download are untouched. `filter_rows` reads
  as ONE merged table with its dropped input rows in place, tinted, off the verified lineage
  sidecar. The header is one horizontal axis, the same for either shape — the inputs stacked
  vertically, a bracket where there is more than one, a rail, then the output, which is a sibling
  of the stack and so does not move when an input is added. Each unit names its part in words
  (`base input` / `reference input` / `output`), carries the row count of the frame it names, and
  links that frame's raw full-rows view (`?raw=1`) + CSV download. A frame the diff did not read
  (an `enrich`'s reference where the parquet will not open) is listed with no count, never a
  guessed one. The rail carries `diff.tally` — the list of things the stage did that its own shape
  MEASURED, in one vocabulary (`+2 cols · −3 cols · 0 cells changed`, `−121 rows`); a filter
  compares no cells and no columns, so it reports neither rather than a zero it never took.
  `build_stage_diff`
  returns None — the plain output view — for every other type and whenever alignment can't be
  verified. The **full-rows page** (`…/stage/{sid}/rows`) renders the same partial over
  `MAX_TABLE_ROWS` rows, keeping its row numbers and click-to-expand cells; `?raw=1` forces the
  plain table, and each view names itself and links the other.

## The run log (`_run_log_panel.html` → `app/static/run_log.js`)
One macro, rendered twice per run page: **scoped to the open stage** under the panel's tab
strip, and **unscoped** in its own section at the foot of the page. Both are folded and
connect their SSE feed on first open, so a page nobody unfolds costs no stream and clicking
through the graph leaves no connection behind (the panel's script closes the previous
stage's log through the handle `initRunLog` returns). Every hook the client binds is a `js-`
class, not an id — the two instances share a document.

`GET …/events?stage=<id>` and `…/events/page?stage=<id>` filter server-side
(`app.web.run_events.select_stage_events`): the stage's own events plus `run_done`, which
is what ends a stream. `run_events` is written against a run DIRECTORY, so the same three
functions serve a production run under `runs/` and an eval's subset run under `eval_run/`;
`initRunLog` takes the run's URL prefix as `base` rather than building one. Both the opening tail and "load older" count over the FILTERED events, so a stage
holding a handful of them inside a 270k-event log still opens full. `links.run_log` is None
in the review packet — a folder has no server to tail — and the panel then renders no log.

## One eval, one eval run (`eval_detail.html`, `eval_run.html`)
Both sit in the project shell with **Evals** lit, and both open with the stage panel's own
head — name, status badge, blurb, then a facts line — over ONE tab strip:
- **An eval** is *Runs │ Definition │ Dataset*. Runs leads because it is what the eval has
  actually said, and it draws the **runs index's own table** (`.stages.runs-table`, four
  columns, the run id demoted to the row's link target, the whole row clickable through
  `static/row-link.js` — delegated from the document, so the two pages share one handler).
  What differs is the result cell: an eval run's outcome is its score, so the stored
  accuracy sits where the stage strip does, and a run that stored none shows its status
  badge rather than a percentage nobody measured. Definition opens on compatibility, since
  a broken eval makes every number under it a claim about a workflow that no longer exists.
- **A run** is *Rows │ Scoring │ Pathway*, under a `stat-strip` of rows scored / passed /
  failed / accuracy — all four COUNTED off the run's own `result.parquet`, never off the
  stored metrics, so the tiles and the table cannot disagree. Rows is the comparison
  itself (`app.web.eval_run_view`): the verdict, then each check's expected/actual pair
  with a mismatch tinted, then the eval-dataset columns the model was given. Those columns
  are position-aligned, so a dataset whose length no longer matches the run is dropped with
  the reason stated rather than lined up wrongly. The scored checks are read off the result
  table, not the config — the config may have moved since. `failures only` hides the passing
  rows client-side; the counts above stay the run's.
- The run's own **event log** is the run page's log panel, unchanged, over
  `…/evals/<id>/runs/<run>/events`. A run still executing carries the panel before it has
  logged anything — that is what shows the log filling. A vetoed run executed nothing, so it
  has no log and the panel is absent rather than empty.
- A **run in flight** (`running`) has no result table, so it shows no strip, no rows and no
  metrics: each pane says it is still executing, and the facts line counts the time it has
  been running instead of a duration it took.

## How a step was checked — both statements live in Transform
Beside the thing each one is a verdict on, and never as a section of its own:
- **`_stage_certification.html`** opens **🧪 Example behavior**, because what it claims is
  about those examples. It used to sit under the summary, a section above the cases it
  was talking about.
- **`worked examples (evals)`** (`app.web.eval_coverage` → `_stage_eval_check.html`) is an
  `h3` INSIDE whichever transform block the stage has — a peer of that block's other
  headings, not a fifth `.exec-block` competing with them. An eval may target any stage,
  so the LLM block and all three authored-code blocks carry it.

The **LLM block reads in the order one call happens**: what the model is asked → what it
sees, per row → expected answer shape → worked examples (evals) → settings. The dials come
last because they are the least of what a reviewer is judging. An eval is the only check
that reaches an `llm_transform`'s answers at all — `build_certification` returns None
without an authored code block, so those stages carry no certification.

**ONE ROW PER EVAL**, because two evals score different datasets: their row counts do not
add up and their accuracies do not average, so any single figure over both would be a
number nobody measured. Worst first. The caveat — that nothing here speaks for rows outside
those sets — is stated once above the table, not repeated under every row.

Coverage attaches to an eval's **target** stage alone; the rest of the pathway executed,
but nothing compared what it produced to anything. **Staleness outranks the score**: a
stale row's result cell reads `stale` and carries no figure, since the figure is a verdict
on code that has moved — the version it did score is its own column. Which version counts
as current differs by surface: a run panel uses the version THAT RUN pinned (exact), the
node panel the latest stored version (the test `eval_status` already applies, since a
working copy is not a version). A step no eval targets renders nothing.

## Live progress + the stage simulator
`POST /project/<m>/run` → `prepare_run` (initial `running` manifest) → background thread →
redirect; `run_detail.html` polls `…/status` every 2s and updates the graph in place, reloading
once on the terminal transition.

An eval run takes the same shape: `POST …/evals/<id>/run` → `start_eval_run` (validated
synchronously, so an incompatible eval still answers 400 and an unknown version 404, then an
initial `running` EvalRun) → daemon thread → redirect. `eval_run.html` polls
`…/evals/<id>/runs/<run>/status` every 2s **only** while the record reads `running`, moving the
elapsed figure and reloading once at `terminal` — where the badge, the metrics and the scored
rows all arrive together.

**The simulator** is its own page — `…/stage/<sid>/simulate` (`run_stage_simulate.html`): the
folded transform, the input rows with per-row checkboxes, the controls, then the result, one
column. Picking and reading the answer used to straddle two tabs of the run panel, which moved
the reader off the rows they had just picked. The panel now links it from **Transform** and
holds no picker. Running it posts `…/stage/<sid>/preview` (`runtime/preview.py`), which runs the
handler **in memory** and persists nothing; refused for `publish`/`human_review_queue`/
`input_data` (side effects), and the page 404s for those types and for an unreadable version.

Every stage definition a run page shows or executes (panel, lineage panel, simulator)
comes from the version the run pinned, via `services.run.load_pinned_stage_def` /
`load_run_workflow` — never the working copy.
Unresolvable version → the panels show a stated reason in place of the definition and the
in-memory re-run returns 409 rather than executing the working copy.
