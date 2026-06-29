# Architecture — the code map

A FastAPI app over a file-backed methodology artifact. No database; everything is
files under `examples/<name>/`. Three features (runner, compiler, eval) meet at one
clean contract module.

## The clean interface: `app/dag_schema.py`

THE canonical contract. It defines what a valid stage, DAG, and **named schema**
look like, plus the column-type vocabulary. **Dependency rule (deliberate): it
imports nothing from the runtime or the compiler.** The runtime imports it to
validate/execute; the compiler imports it to know what to emit; neither imports the
other. Keep it pure data + pure functions.

What lives here:
- The **7 node types** and the executable-handle block each requires
  (`input_data`, `llm_transform`, `python_transform`, `join`, `aggregate`,
  `human_review_queue`, `publish`).
- `validate_stage` / `validate_dag` / `validate_methodology` — stage-spec validation.
- **Named-schema contract** (newer): `SCHEMA_KINDS`, `validate_named_schema`,
  `validate_schema_library`, `exclusive_arcs` validation. See [named-schemas.md](named-schemas.md).
- **Eval contract** (newer): `validate_eval_spec`, `build_ground_truth_schema`.
- Prose companion: `app/SCHEMA.md`.

Distinct from RUNTIME DATA validation (`app/runtime/validation.py`), which checks
actual dataframes against a schema at run time. `dag_schema.py` checks the *spec*.

## `app/runtime/` — the Runner

Executes a DAG. → see `app/runtime/AGENTS.md` for detail.
- `runner.py` — `execute_run` / `prepare_run` / `run_prepared` / `resume_run`.
  `_execute_stages` is the core loop: per stage it validates inputs, runs the
  handler, validates outputs, writes the output + updates `manifest.json` **mid-run**
  (so the UI shows live progress). Stage statuses: `pending` → `running` →
  `ok` | `validation_warnings` | `error` | `awaiting_review`. On a review halt it
  stops and marks downstream `pending`; **resume re-runs any non-complete stage**
  (this is what powers both "resume after review" and "re-run failed stages").
- `handlers.py` — one handler per node type.
- `llm.py` / `llm_agent_sdk.py` / `llm_mock.py` — LLM backends. `call_llm_real`
  shells out to `claude -p`; backend is selectable (`CW_LLM_BACKEND`, or
  `CW_LLM_FORCE_MOCK=1`). The mock is deterministic and offline.
- `validation.py` — dataframe-vs-schema checks. `preview.py` — scratch re-runs.

## `app/compiler.py` — the Compiler

Distills prose or an unstructured transcript into a *draft* DAG (emits to the
`dag_schema` contract). Surfaced at `/compile`.

## `app/main.py` — the FastAPI app + all routes

The web layer. Key areas:
- **Methodology + DAG views**: index, `/methodology/{m}` (DAG), stage detail.
- **Data model view** (newer): `load_schemas`, `build_schema_er_diagram`,
  `/methodology/{m}/schemas`. `list_methodologies` recognizes a methodology that
  has a data model but no DAG yet.
- **Run views**: run detail + the live-progress status JSON poller, the per-run
  stage panel, scratch preview. See [run-and-review-ui.md](run-and-review-ui.md).
- **Review queue**: `queue_page` (now recovers + renders the MODEL INPUT), `decide`,
  `resume`.
- `build_mermaid_graph` renders the DAG with per-stage status strokes.

Templates in `app/templates/`, styles in `app/static/style.css`. The DAG/ER
diagrams are Mermaid, rendered client-side.

## Repo layout

```
app/dag_schema.py     the contract (the interface)         ← start here
app/SCHEMA.md         prose schema spec
app/runtime/          the Runner                            → app/runtime/AGENTS.md
app/compiler.py       the Compiler (transcript/prose → draft DAG)
app/main.py           FastAPI app (routes)                  → app/AGENTS.md
app/templates/, app/static/   the web UI
examples/<name>/      methodology artifacts                 → examples/*/AGENTS.md
  schemas/   data model (named schemas, authored first)
  compiled/  DAG stages (authored second)
  eval/      ground truth + eval specs (separate from generation)
  runs/      persisted runs + manifest.json
docs/        this documentation
```

## Running it

```
pip install -r requirements.txt          # fastapi, pandas, pyarrow, pyyaml, claude-agent-sdk, ...
python -m uvicorn app.main:app --port 8765   # web UI
python -m app.runtime.runner examples/<name> # run a methodology from the CLI
```

Note: uvicorn without `--reload` does NOT hot-reload Python (templates do reload
per request). Restart the server after editing `app/*.py`.

## Conventions (load-bearing, not stylistic)

- **Never fabricate.** Unsourceable → `null`/`unknown`; fail loudly or halt.
- **Every value carries provenance** — its source travels with it.
- **Gate the expensive/irreversible step** behind `human_review_queue` (halts;
  decisions are content-hashed so they survive re-runs).
- **Adversarially verify LLM output** before it becomes an asset.
