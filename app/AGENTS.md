# app — the FastAPI web app (workflow / run / review UI)

`app/main.py` is a thin bootstrap: creates the FastAPI app, mounts `/static`, includes the
routers in `app/web/routers/`, which import the Runner (`app.runtime`) and the schemas
(`app.models`) and share `app/web/{config,loading,diagrams}`. Run:
`python -m uvicorn app.main:app --port 8765`.

## Pages / routes
- `/` project list · `/project/<m>` the project shell (Overview · Document · Data model ·
  Workflow · Runs); the Workflow section carries the mermaid graph + inline node review
  (`/project/<m>/node/<id>/review-partial`).
- `/project/<m>/runs`, `/runs/<id>` — run history + detail.
- `/project/<m>/runs/<id>/queue/<stage>` — the human-review queue UI (+ `/decide`, `/resume`).

## The run page's issue index (`app.web.run_issues` → `_run_issues.html`)
Between the header and the graph, an INDEX into the stage panels — every entry is one line
plus a deep link, and the detail stays in the panel's own validation block.
- **"Why this run stopped"** — one card per `error` stage, leading with which story it is,
  because they route to different people: a schema refusal (`OutputSchemaViolation`) and an
  authored `StepRefused` are the data's and link the panel's **Data** / **Transform** tab; any
  other exception is the code's and keeps its type, message and traceback. Each card names the
  stages downstream of it that never ran, read off the pinned version's edges — with no readable
  version it names none rather than blaming the pending stages it can see.
- **The flagged section**, titled by its own counts (`17 warnings, 2 errors`; a severity with
  none of them is left out) — every issue the cards do not carry (warnings anywhere, plus an
  error-severity INPUT issue, which only warns its stage), one line per stage × column ×
  message, stages in the run's own order. Folded when something stopped the run; open when
  nothing did, which is the run whose warnings would otherwise go unread.

A deep link loads the panel through `_loadStage(id, {tab, reveal})`, which the panel serves by
publishing `_selectTab` on its root element. `reveal` smooth-scrolls the panel's top to the top of the
run column; every click the reader aims — the guide's steps and output links, an issue link, a
graph node — uses it, and a load they did not ask for (deep link, panel self-refresh) does not.

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
