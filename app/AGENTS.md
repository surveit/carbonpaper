# app — the FastAPI web app (DAG / run / review UI)

`app/main.py` serves the methodology DAGs, their runs, and the review queue. It
imports the Runner (`app.runtime`) and the contract (`app.dag_schema`). (The
Compiler feature adds `/compile` pages — see the "Compiler" section appended in
that PR.)

Run: `python -m uvicorn app.main:app --port 8765`.

## Pages / routes
- `/` — methodology list. `/methodology/<m>` — the DAG (mermaid) + click-through stage detail.
- `/methodology/<m>/data-model` — ER diagram of stage schemas.
- `/methodology/<m>/runs`, `/methodology/<m>/runs/<id>` — run history + a run's detail.
- `/methodology/<m>/runs/<id>/queue/<stage>` — the human-review queue UI (+ `/decide`, `/resume`).

## The node / stage panel — 3 tiers, left → right
`run_stage_partial` → `_run_stage_panel.html` renders **Inputs │ Transform │ Outputs**:
- **Inputs** — each input's schema + the upstream data preview, with per-row checkboxes for the scratch re-run.
- **Transform** — the *raw* executable handle (`_stage_executable.html`): the llm prompt+model+tools, the python function code, join keys, aggregate ops, connector/queue/publish spec.
- **Outputs** — the output schema + output data preview + **validation rendered as part of the output** (input + output issues from the manifest, marked error/warning).
- **Compiler notes are deliberately NOT shown here** — they belong on the `/compile` pages. (Validation ≠ compiler notes.)
- URL-valued cells render as full clickable links (`cell-url`), never truncated.

## Live run progress (no full-page reload)
`POST /methodology/<m>/run` → `runner.prepare_run` (writes an initial `running`
manifest) → executes in a **background thread** (`_run_in_background`) → redirects
immediately. `run_detail.html` polls `GET …/runs/<id>/status` every 2s and updates
the status badge, counts, and the mermaid DAG *in place*; on the terminal
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
`main.py` (routes) · `templates/` (`run_detail.html`, `_run_stage_panel.html`,
`_stage_executable.html`, `_stage_content.html`, + base/index/methodology/queue/…)
· `static/style.css` · `runtime/preview.py` (scratch-run backend).

---

# Compiler — `/compile` (transcript / prose → draft DAG)

The third feature on the DAG artifact. `app/compiler.py` distills an unstructured
Claude Code run (a transcript `.jsonl`) — or prose — into a *draft* methodology
that conforms to `app/dag_schema`. **Separation rule:** the compiler imports only
`app.dag_schema` + `claude_agent_sdk`; it must NOT import `app.runtime` (the
runner stays ignorant of the compiler). `app/main.py` (the app shell) may wire the
`/compile` routes.

## How it works (`app/compiler.py`)
- `parse_transcript` — extract the tool-call sequence (WebSearch/WebFetch/Bash) + the final report from a transcript jsonl.
- `compile_from_transcript` — build a prompt that frames the node-type contract straight from `dag_schema.NODE_TYPES` (so it can't drift), and asks the model (Agent SDK) to emit a methodology as JSON: stages valid per the contract + `methodology_raw.md` + compiler_notes. Bounded retry on malformed JSON.
- `validate` — runs `dag_schema.validate_methodology` on the output as a self-check.
- `harvest_eval_fixtures` — pulls (search→chosen-url) and (doc→fields) pairs from the transcript as eval seeds.

## A compilation is a first-class object (parallels a run)
Persisted at `compilations/<id>/` (gitignored): `manifest.json` (status, model,
n_stages, validation_issues, stage_summary) + `what_happened.json` (parsed tool
sequence + the LLM prompt sent + the raw response + validation) + `dag/` (the
generated `compiled/*.yaml` + `methodology_raw.md`) + `eval_fixtures.jsonl`.

## Pages
`GET /compile` (list of compilations) · `GET /compile/new` (form: pick transcript /
out-name / model) · `POST /compile/new` (runs in a background thread, redirects) ·
`GET /compile/<id>` (the object view: **Input** · **What happened** · **DAG output**
with mermaid). Sample output is committed at `examples/_compiled_sungai_lilin/` (a
draft DAG the compiler produced from the sungai_lilin transcript; validates clean
and independently reproduced the locate/extract/adjudicate backbone).

