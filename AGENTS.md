# prototype_one — reviewable AI methodology DAGs

A platform for running data/OSINT pipelines as **DAGs of typed nodes** instead of
opaque generic code. A "methodology" is a directed graph whose every edge is
schema-validated, whose expensive/irreversible steps sit behind human-review gates,
and whose runs are persisted with a full manifest — an AI-driven pipeline that is
*testable and reviewable*, not a black box. It exists to serve journalism and
institutional accountability, which is why the conventions below are the product,
not preferences.

## 📖 Documentation index (`docs/`)

New here? Read in this order:

1. **[docs/overview.md](docs/overview.md)** — product context: the mission, what a
   methodology is, the data-model-first thesis, the three features, the LobbyMap
   flagship. *Start here.*
2. **[docs/architecture.md](docs/architecture.md)** — the code map: the `dag_schema`
   contract, the runtime, the compiler, the app, the repo layout, how to run it.
3. **[docs/named-schemas.md](docs/named-schemas.md)** — the data model + eval data
   model (the core recent concept). Read before touching schemas or eval.
4. **[docs/run-and-review-ui.md](docs/run-and-review-ui.md)** — the run page + review
   queue UX and where the code lives.
5. **[docs/lobbymap-eval.md](docs/lobbymap-eval.md)** — the LobbyMap reproduction
   project: methodology, ground truth, the Level-2 run, how to extend, constraints.

Per-directory detail: `app/AGENTS.md`, `app/runtime/AGENTS.md`, `examples/*/AGENTS.md`.
Example-specific notes: `examples/lobbymap/{RESEARCH,LEARNINGS,LEVEL2}.md`.

## The one file to understand first

`app/dag_schema.py` — THE canonical contract (the 7 node types + the named-schema +
eval contracts + validators). It imports nothing from the runtime or compiler; both
meet here. Keep it pure. Prose companion: `app/SCHEMA.md`.

## Conventions (load-bearing, not stylistic)

- **Never fabricate.** A value that can't be sourced is `null`/`unknown`; the
  pipeline fails loudly or halts rather than inventing a number, URL, or citation.
- **Every value carries provenance** — its source travels with it.
- **Gate the expensive/irreversible step** behind `human_review_queue` (halts the
  run; decisions are content-hashed so they survive re-runs).
- **Adversarially verify LLM output** before it becomes an asset; demote/drop the
  unverified.
- **Eval never leaks into generation** — the eval data model derives from the
  generation schema, one-directional.

## Run it

```
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8765        # web UI
python -m app.runtime.runner examples/<name>      # CLI run
```
(uvicorn without `--reload` does not hot-reload Python — restart after editing `app/*.py`.)
