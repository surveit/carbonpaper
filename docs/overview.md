# Overview — what this is and why

## The mission (product context)

This project serves **journalism and institutional accountability**: tools that
find, verify, and surface true things about how power and money actually work. The
engineering standards are the product, not preferences — a fabricated number or an
unsourced claim doesn't just lower quality, it defeats the purpose.

Two load-bearing rules flow from that and appear throughout the code:

- **Never fabricate; fail loudly instead.** A value that can't be sourced is
  `null`/`unknown`. The pipeline halts or errors rather than inventing a number,
  URL, citation, or quote. Concretely: the LLM backends never silently fall back
  to the mock (`app/runtime/options.py`), the runner rejects duplicate input rows
  instead of guessing intent, and the review queue says "reviewing blind" when it
  can't recover the model's input rather than hiding it.
- **Expensive or irreversible steps sit behind human review.** The
  `human_review_queue` stage halts the run; decisions are content-hashed so they
  survive re-runs and LLM non-determinism.

## What a "methodology" is

A **methodology** is a directed acyclic graph (DAG) of typed nodes — a data/OSINT
pipeline expressed as a reviewable artifact instead of opaque generic code. Every
edge is schema-validated; runs are persisted with a full manifest. The result is an
AI-driven pipeline that is **testable and reviewable, not a black box**.

A methodology lives in a folder `examples/<name>/`:
- `compiled/*.yaml` — the **DAG** (one file per stage).
- `methodology_raw.md` (or `.txt`) — the prose the DAG was distilled from.
- `code/` — python modules the `python_transform` and `publish` stages call.
- `data/` — committed input snapshots.
- `runs/<run_id>/` — persisted run outputs + `manifest.json`.
- `decisions/` — content-hashed human-review decisions.
- `versions/<version_id>/` — frozen snapshots of the compiled DAG (see
  [run-and-review-ui.md](run-and-review-ui.md)).

## The three features on the DAG artifact

| Feature | Code | Status |
|---|---|---|
| **Runner** | `app/runtime/` | On master. Executes a DAG: validates I/O between stages, persists outputs + `manifest.json`, halts for human review, resumes. |
| **Compiler** | — | *Not on master.* Distills prose or an unstructured transcript into a draft DAG; lives in the open PR stack. Don't look for `app/compiler` on master. |
| **Eval** | `app/models/eval.py` | Data model only. `EvalConfig` (a row-aligned case table injected at one stage, compared at another) and the grain-preservation gate exist as validated models; no runner integration or committed eval configs yet. |

## The examples

Three methodologies are committed as `examples/<name>/`, each a real end-to-end
exercise rather than a toy:

- **`lobbymap/`** — reproduces the shape of InfluenceMap's LobbyMap (scoring
  corporate climate-policy engagement per (query × data source) cell against a
  benchmark). The original domain the stage vocabulary was designed for.
- **`congresswatch/`** — the same pipeline shape ported to US Congress press
  releases + lobbying filings, built to stress-test the platform on a second
  domain. Its findings memo is `examples/congresswatch/FINDINGS.md`, and the
  resulting product critique is [RETHINK.md](RETHINK.md) — read that for where
  the product needs to go (discovery views, not per-entity scorecards).
- **`drift/`** — a geospatial example (GeoJSON inputs).

## Where to go next

- New to the code? → [architecture.md](architecture.md) (the code map).
- Working on the data model / schemas? → [named-schemas.md](named-schemas.md).
- Working on the run/review UI? → [run-and-review-ui.md](run-and-review-ui.md).
- Product direction? → [RETHINK.md](RETHINK.md) (the post-CongressWatch critique).
- Storage + model wiring plan? → [models-and-storage.md](models-and-storage.md).
