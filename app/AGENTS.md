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

## The stage panel — 3 tiers (`run_stage_partial` → `_run_stage_panel.html`)
**Inputs │ Transform │ Outputs**:
- **Inputs** — each input's schema + upstream preview, per-row checkboxes for the scratch re-run.
- **Transform** — the *raw* executable handle (`_stage_executable.html`): llm prompt+model+tools,
  python code, join keys, aggregate ops, connector/queue/publish spec.
- **Outputs** — output schema + preview + **validation rendered as part of the output** (input
  + output issues from the manifest). URL cells are full clickable links. Compiler notes live
  on `/compile`, not here.

## Live progress + scratch re-run
`POST /project/<m>/run` → resolve version + load its pinned stages → `prepare_run`
(initial `running` manifest) → background thread →
redirect; `run_detail.html` polls `…/status` every 2s and updates the graph in place, reloading
once on the terminal transition. Scratch: pick N input rows → `…/stage/<sid>/preview`
(`runtime/preview.py`) runs the handler **in memory**, persists nothing; refused for
`publish`/`human_review_queue`/`input_data` (side effects).
