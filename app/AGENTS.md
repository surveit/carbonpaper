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

## The stage panel — two tabs (`run_stage_partial` → `_run_stage_panel.html`)
**Data │ Transform** (under the Schema | Current-run tier switch):
- **Data** — what goes in and what comes out, one pane. Schema tier: the input schemas, then
  the output schema. Current-run tier: the stage's output — rendered as a **diff against its
  input** where the stage type permits one (below) — then validation **as part of the
  output** (input + output issues from the manifest), then the upstream input previews with
  the per-row checkboxes for the scratch re-run, folded in an `input rows` disclosure. URL
  cells are full clickable links. Compiler notes live on `/compile`, not here.
- **Transform** — the *raw* transform config block (`_stage_executable.html`): llm prompt+model+tools,
  join keys, aggregate ops, connector/queue/publish spec — plus the scratch re-run
  result. An authored-code block (`function` / `filter`) reads **description → examples → code**:
  the block's plain-language `summary` leads, the test cases follow, and the source is rendered
  last and folded (`_stage_code.html`), because the reviewer is a journalist, not an engineer.
  One pane serves **both** the Schema and Current-run tiers (its `data-pane` names both):
  the definition the run pinned *is* what the current run transformed with, so switching tiers
  here would show the same thing twice and push you off "Current run" mid-panel.
- **The diff** (`app.web.stage_diff` → `_stage_diff.html`): a 1:1 stage
  (`python_row_function`, `llm_transform`) reads as a positional diff (added columns tinted,
  changed cells marked); `filter_rows` reads as ONE merged table with its dropped input rows
  in place, tinted, off the verified lineage sidecar. The diff header names `input → stage`
  and links both raw frames' full-rows views + CSV downloads. `build_stage_diff` returns
  None — the plain output view — for every other type and whenever alignment can't be verified.

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
