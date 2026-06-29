# Overview — what this is and why

## The mission (product context)

This project serves **journalism and institutional accountability**: tools that
find, verify, and surface true things about how power and money actually work. The
engineering standards are the product, not preferences — a fabricated number or an
unsourced claim doesn't just lower quality, it defeats the purpose.

Two load-bearing rules flow from that and appear throughout the code:

- **Never fabricate; fail loudly instead.** A value that can't be sourced is
  `null`/`unknown`. The pipeline halts or errors rather than inventing a number,
  URL, citation, or quote. (See `validate_*` in `app/dag_schema.py`, the `NS`/`NA`
  status modeling in lobbymap's `cell_score`, and the "reviewing blind" notice in
  the review queue.)
- **Every value carries provenance.** Each row/figure travels with its source so
  the path from source to conclusion is reproducible.

## What a "methodology" is

A **methodology** is a directed acyclic graph (DAG) of typed nodes — a data/OSINT
pipeline expressed as a reviewable artifact instead of opaque generic code. Every
edge is schema-validated; expensive/irreversible steps sit behind human-review
gates; runs are persisted with a full manifest. The result is an AI-driven
pipeline that is **testable and reviewable, not a black box**.

A methodology lives in a folder `examples/<name>/`:
- `schemas/*.yaml` — the **data model** (named schemas), authored first.
- `compiled/*.yaml` — the **DAG** (one file per stage), authored second.
- `eval/*` — ground truth + eval specs (kept separate from generation).
- `runs/<run_id>/` — persisted run outputs + `manifest.json`.
- prose/code/data the stages reference.

## The big idea this codebase is currently built around: data-model-first

The prototype began **DAG-first** — the data model was *derived from* the DAG (each
stage's `output_schema`). You couldn't model your data without first authoring the
pipeline. This was inverted: **named schemas** make the data model a first-class
artifact authored *before* the DAG. See [named-schemas.md](named-schemas.md). This
is the conceptual spine of the recent work.

## The three features on the DAG artifact

| Feature | Code | What it does |
|---|---|---|
| **Runner** | `app/runtime/` | Executes a DAG: validates I/O between stages, persists outputs + `manifest.json`, halts for human review, resumes. |
| **Compiler** | `app/compiler.py` | Distills prose or an unstructured transcript into a *draft* DAG. |
| **Eval** | `app/dag_schema.py` + `examples/*/eval/` | Checks a methodology reproduces ground truth. The eval *data model* is now formalized (ground truth derived from the generation schema); the running harness is example-level (see lobbymap). |

## The flagship example: LobbyMap reproduction

`examples/lobbymap/` is the real exercise: reproduce **InfluenceMap's LobbyMap**
(which scores corporate climate-policy engagement on a −2..+2 matrix) closely
enough to build an eval dataset from their published results and test ourselves
against it. This is where the platform meets a real, contested, sourced problem.
See [lobbymap-eval.md](lobbymap-eval.md).

## Where to go next

- New to the code? → [architecture.md](architecture.md) (the code map).
- Working on the data model / schemas? → [named-schemas.md](named-schemas.md).
- Working on the run/review UI? → [run-and-review-ui.md](run-and-review-ui.md).
- Working on the eval / lobbymap? → [lobbymap-eval.md](lobbymap-eval.md).
