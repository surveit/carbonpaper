# app — the FastAPI web app (DAG / run / review UI)

`app/main.py` serves the methodology DAGs, their runs, and the review queue. It
imports the Runner (`app.runtime`) and the contract (`app.models`). (The
Compiler feature adds `/compile` pages, split into `app/pages.py` +
`app/api/compile.py` — see the "Compiler" section below.)

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
`main.py` (app shell + run/methodology/review routes) · `web_context.py` (shared
templates / paths / type maps / `build_mermaid_graph` / background runner) ·
`pages.py` + `api/compile.py` (the compiler's page + action routes) · `templates/`
(`run_detail.html`, `_run_stage_panel.html`, `_stage_executable.html`,
`_stage_content.html`, + base/index/methodology/queue/compile…) ·
`static/style.css` · `runtime/preview.py` (scratch-run backend).

---

# Compiler — `/compile` (unstructured account → draft DAG)

The third feature on the DAG artifact. `app/compiler.py` reads an UNSTRUCTURED
input — a captured agent/tool transcript, working notes, or plain prose — **as
prose** and asks the model to distill it into a *draft* methodology that conforms
to `app/models`. It does no structured parsing of the input; the model recovers
the pipeline. **Separation rule:** the compiler imports only `app.models`,
`app.prompt`, + `claude_agent_sdk`; it must NOT import `app.runtime` (the runner
stays ignorant of the compiler). The `/compile` routes live in `app/pages.py`
(pages) + `app/api/compile.py` (actions), mounted on the app in `main.py`.

## How it works
- `app/prompt.py` — `SYSTEM_PROMPT` + `build_compile_prompt(input_text, name)`. The
  node-type contract is rendered straight from `models.NODE_TYPES` so the prompt
  can't drift from the real schema.
- `compiler.read_input` — read the file as text (a `.jsonl` transcript is fed to the
  model as prose, exactly like a `.md`/`.txt` note).
- `compiler.compile_methodology` — one no-tools Agent-SDK call, then parse the JSON
  (`stages` + `methodology_raw_md` + `compiler_notes`) with bounded retry on
  malformed output. Never fabricates: a bad/empty result raises rather than being
  passed off as a clean compile.
- `compiler.validate` — runs `models.validate_methodology` as a self-check.

## A compilation is a first-class object (parallels a run)
Persisted at `compilations/<id>/` (gitignored): `manifest.json` (status, model,
n_stages, validation_issues, stage_summary) + `what_happened.json` (the input
excerpt + the LLM prompt sent + the raw response + compiler_notes) + `dag/` (the
generated `compiled/*.yaml` + `methodology_raw.md`).

## Pages
`GET /compile` (list of compilations) · `GET /compile/new` (form: pick input /
out-name / model) · `POST /compile/new` (runs in a background thread, redirects) ·
`GET /compile/<id>` (the object view: **Input** · **What happened** · **DAG output**
with mermaid). The CLI — `python -m app.compiler <input> <out_name>` — writes its
scratch output to the gitignored `examples/_compiled_<name>/`.

