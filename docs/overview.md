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

## The vocabulary (locked 2026-07-04; see [/plans/naming-refactor.md](../plans/naming-refactor.md))

- A **project** is the container: the folder `examples/<name>/` holding
  everything below.
- A **methodology** is the authored **prose** method (`methodology_raw.md`) —
  the thing a journalist writes.
- A **workflow** is the executable stage graph the methodology compiles into
  (`compiled/<NN>_<stage_id>.json`, one file per stage) — a directed acyclic
  graph of typed stages, every edge schema-validated.

A project directory also holds `code/` (python modules stages call), `data/`
(input snapshots), `runs/<run_id>/` (persisted outputs + `manifest.json`),
`decisions/` (content-hashed review decisions), and `versions/<version_id>/`
(frozen workflow snapshots). Project directories are **runtime data, not
source** — `examples/` is untracked; the historic example projects (lobbymap,
congresswatch, drift) live on disk and in git history.

## The three features on the workflow artifact

| Feature | Code | Status |
|---|---|---|
| **Runner** | `app/runtime/` | On master. Executes a workflow: typed `Stage` objects end-to-end, validates I/O between stages, persists outputs + `manifest.json`, halts for human review, resumes. |
| **Compiler** | `app/compiler/` | Engine on master: prose → LLM → validated workflow, re-asking the LLM on schema-validation failure (`python -m app.compiler`; persistence in `app/services/compilation.py`). The authoring *UI* is in the open PR stack. |
| **Eval** | `app/models/eval.py` | Data model only. `EvalConfig` (a row-aligned case table injected at one stage, compared at another) and the grain-preservation gate exist as validated models; no runner integration or committed eval configs yet. |

## Where the product needs to go

[/plans/RETHINK.md](../plans/RETHINK.md) — written after running the pipeline
shape on a second domain (US Congress + lobbying) — is the standing product
critique: the platform serves the workflow *author* well and the journalist
barely at all, because the journalism questions are cross-entity ("who's the
outlier?") while the outputs are per-entity. Read it before adding
operator-facing features. It's a `/plans` scratch note, not a spec — don't cite
it for how the code works today.

## Where to go next

- New to the code? → [architecture.md](architecture.md) (the code map).
- Working on the data model / schemas? → [named-schemas.md](named-schemas.md).
- Working on the run/review UI? → [run-and-review-ui.md](run-and-review-ui.md).
- Vocabulary and its rationale (historical)? → [/plans/naming-refactor.md](../plans/naming-refactor.md).
- Storage convention? → [models-and-storage.md](models-and-storage.md).
