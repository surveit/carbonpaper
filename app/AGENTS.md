# app — the FastAPI web app (workflow / run / review UI)

The web app serves the workflows, their runs, and the review queue.
`app/main.py` is a thin bootstrap: it creates the FastAPI app, mounts `/static`,
and includes the routers in `app/web/routers/`. Those routers import the Runner
(`app.runtime`) and the schemas (`app.models`), and share the helpers in
`app/web/` (`config`, `loading`, `diagrams`). (The Compiler feature adds
`/compile` pages — see the "Compiler" section appended in that PR.)

Run: `python -m uvicorn app.main:app --port 8765`.

## Pages / routes
- `/` — project list. `/project/<m>` — the project shell (Overview · Document · Data
  model · Workflow · Runs); the Workflow section carries the mermaid graph + inline
  click-through node review (`/project/<m>/node/<id>/review-partial`).
- `/project/<m>/runs`, `/project/<m>/runs/<id>` — run history + a run's detail.
- `/project/<m>/runs/<id>/queue/<stage>` — the human-review queue UI (+ `/decide`, `/resume`).

## The node / stage panel — 3 tiers, left → right
`run_stage_partial` → `_run_stage_panel.html` renders **Inputs │ Transform │ Outputs**:
- **Inputs** — each input's schema + the upstream data preview, with per-row checkboxes for the scratch re-run.
- **Transform** — the *raw* executable handle (`_stage_executable.html`): the llm prompt+model+tools, the python function code, join keys, aggregate ops, connector/queue/publish spec.
- **Outputs** — the output schema + output data preview + **validation rendered as part of the output** (input + output issues from the manifest, marked error/warning).
- **Compiler notes are deliberately NOT shown here** — they belong on the `/compile` pages. (Validation ≠ compiler notes.)
- URL-valued cells render as full clickable links (`cell-url`), never truncated.

## Live run progress (no full-page reload)
`POST /project/<m>/run` → `runner.prepare_run` (writes an initial `running`
manifest) → executes in a **background thread** (`run_in_background`) → redirects
immediately. `run_detail.html` polls `GET …/runs/<id>/status` every 2s and updates
the status badge, counts, and the mermaid workflow *in place*; on the terminal
transition it reloads once to render previews/artifacts. Terminal runs don't poll.

## Scratch in-memory re-run (ephemeral, nothing persisted)
Pick N input rows in the Inputs tier → `POST …/runs/<id>/stage/<sid>/preview`
→ `app/runtime/preview.py` `run_stage_preview` loads the run's upstream output(s),
subsets to those rows, runs the stage handler **in memory**, and returns the
output rows (rendered inline in Outputs). It writes nothing and does not touch the
manifest — a debugging scratchpad. For `llm_transform` it makes real (few) LLM
calls; for `publish`/`human_review_queue`/`input_data` it's refused (those handlers
have side effects).

## Files
`main.py` (app bootstrap — mounts static + includes routers) ·
`web/routers/{project,runs,review}.py` (route handlers) ·
`web/{config,loading,diagrams}.py` (paths+templates · fs reads & stage-dict
helpers · mermaid/ER builders) · `web/templates/` (`run_detail.html`,
`_run_stage_panel.html`, `_stage_executable.html`, the `section_*.html` shell
bodies, + base/index/queue/…) · `web/static/style.css` · `runtime/preview.py`
(scratch-run backend).
