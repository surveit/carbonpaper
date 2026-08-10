# app — the FastAPI web app (workflow / run / review UI)

`app/main.py` is a thin bootstrap: creates the FastAPI app, mounts `/static`, includes the
routers in `app/web/routers/`, which import the Runner (`app.runtime`) and the schemas
(`app.models`) and share `app/web/{config,loading,diagrams}`. Run:
`python -m uvicorn app.main:app --port 8765`.

## Pages / routes
- `/` project list · `/project/<m>` the project shell (Overview · Document · Data model ·
  Workflow · Runs); the Workflow section carries the mermaid graph + inline node review
  (`/project/<m>/node/<id>/review-partial`).
- `/project/<m>/runs`, `/runs/<id>` — run history + detail. `/runs/new` is the
  run-launch form (version picker, one path field + row cap per file input, and an
  **Advanced** fold holding the row-level cache checkbox) — the one surface where a run
  is configured, which the history page's ▶ New run and the Workflow page's ▶ Run
  workflow both link to. Registered ahead of `/runs/<id>`, which would otherwise read
  `new` as a run id. The fold is closed on load and `<details>` submits its content
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
`.run-nav` (360px, collapsible) then `.run-main`. The nav column is the **spine** and holds
the review guide alone — so it is rendered **only** where the pinned version carries a guide,
and the work column takes the whole width otherwise (`.run-shell.no-nav`). Writing a guide is
offered on the version page, which is where one is stored. The work column is four named
sections, in this order:

1. **Run overview** — the header (grounding line, CTA, status bar) and the issue index.
   Everything about the run; nothing that is its result.
2. **Run outputs** — the files a publish stage wrote, off `header.artifacts`, as links.
   Absent when there are none. Never a button: a CTA is an imperative, a run that finished
   clean has none (`choose_run_cta` returns an empty `RunCta`), and a primary button sized
   to a filename was the widest thing on the page.
3. **Workflow** — the minimap.
4. **Stage details** — the stage panel. `hidden` until `loadStage()` unhides it: before a
   stage is picked there is nothing to show, and a heading over an empty box is exactly the
   kind of always-on furniture this page was cut down to remove.

The **toolbar** (`.run-toolbar`) shares Run overview's heading line (`.run-overview-head`,
heading left, actions right — neither spends a row of its own): the review-packet export as a
link, and beside it a menu button holding the raw manifest and the whole-run log
(`.run-audit-menu`, closed on load, and closed again by Escape or a click outside it). The
heading line, not the toolbar, is the menu's positioning context, so the menu can be bounded
by the column's width instead of hanging off its left edge into `.run-main`'s clip.
Neither of those two is part of judging the run, so neither is on the page until the menu is
opened — which is also when the log's SSE feed connects, on a live run as on a finished one.
The export stays outside the menu: handing the packet to someone outside is a thing a reader
comes here to do.

## The run page's issue index (`app.web.run_issues` → `_run_issues.html`)
Inside Run overview, under the header: ONE list indexing the stage panels — every entry is one
line plus a deep link, and the detail stays in the panel's own validation block. It stays in the
work column, not the nav rail: its four-column table needs the width. Drawn with
`_issue_table.html`, the panel + row macros the **Workflow** page's compiler warnings also use;
the macros own the heading (`17 warnings, 2 errors`, a severity with none of them left out), so
neither page can word its counts differently.
- **A stop** — an `error` stage, the run's own end — is the FIRST line, marked `stopped`, its
  message leading with which story it is, because they route to different people: a schema
  refusal (`OutputSchemaViolation`) and an authored `StepRefused` are the data's and link the
  panel's **Data** / **Transform** tab; any other exception is the code's and keeps its type.
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
  output** (input + output issues from the manifest), then the upstream input previews with
  the per-row checkboxes for the scratch re-run, folded in an `input rows` disclosure. URL
  cells are full clickable links. Compiler notes live on `/compile`, not here.
- **Schema** — the static contract: the input schemas, then the output schema.
- **Transform** — the *raw* transform config block (`_stage_executable.html`): llm prompt+model+tools,
  join keys, aggregate ops, connector/queue/publish spec — plus the scratch re-run
  result. An authored-code block (`function` / `filter`) reads **description → examples → code**:
  the block's plain-language `summary` leads, the test cases follow, and the source is rendered
  last and folded (`_stage_code.html`), because the reviewer is a journalist, not an engineer.
- **The diff** (`app.web.stage_diff` → `_stage_diff.html`): a 1:1 stage
  (`python_row_function`, `llm_transform`, `enrich` — against its subject input) reads as a
  positional diff over its INPUT frame as the base: every input column holds its place, one the
  stage dropped struck through carrying the input value, the added ones tinted after them, changed
  cells marked; each header carries a `+` or `−` so both read without colour. `filter_rows` reads
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
(`select_stage_events`): the stage's own events plus `run_done`, which is what ends a
stream. Both the opening tail and "load older" count over the FILTERED events, so a stage
holding a handful of them inside a 270k-event log still opens full. `links.run_log` is None
in the review packet — a folder has no server to tail — and the panel then renders no log.

## Live progress + scratch re-run
`POST /project/<m>/run` → `prepare_run` (initial `running` manifest) → background thread →
redirect; `run_detail.html` polls `…/status` every 2s and updates the graph in place, reloading
once on the terminal transition. Scratch: pick N input rows → `…/stage/<sid>/preview`
(`runtime/preview.py`) runs the handler **in memory**, persists nothing; refused for
`publish`/`human_review_queue`/`input_data` (side effects).

Every stage definition a run page shows or executes (panel, lineage panel, scratch re-run)
comes from the version the run pinned, via `services.run.load_pinned_stage_def` /
`load_run_stages` — never `compiled/`.
Unresolvable version → the panels show a stated reason in place of the definition and the
scratch re-run returns 409 rather than executing the working copy.
