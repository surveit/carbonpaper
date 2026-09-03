# The run manifest

`RunManifest` (`app/models/records/run_manifest.py`) is a run's living record, written
by `app/runtime/executor.py` and read by every screen that reports on a run. This page
holds the reasoning its fields depend on, so the record itself stays short enough to
read in one screen.

## Areas: two prefixes under one collection

A manifest is stored at `run/<project>/<area>/<run_id>`, where `area` is both the run
directory on disk and a segment of the store key.

| Constant | Value | What it holds |
|---|---|---|
| `PRODUCTION_RUNS` | `runs` | a run someone triggered |
| `EVAL_RUNS` | `eval_run` | an eval's subset run |
| `RUN_AREAS` | both, in that order | for a reader totalling a project's whole spend |

`runs/` vs `eval_run/` was the discriminator before the manifest moved into the store,
and keeping it as a key segment means a project's production runs stay one prefix scan
and an eval run can never appear in the runs index.
`app.evals.store.resolve_eval_run_dir` builds its directory from `EVAL_RUNS`, so the
name a run is written under and the name a reader filters by are one string.

## `exclude_unset` is load-bearing, not a size optimisation

`DUMP_OPTS = {"exclude_unset": True}`, so a field the run never set is absent from the
stored payload rather than present and null. Three things depend on that:

- **`clear_halt`** discards `halted_at` from `__pydantic_fields_set__` rather than
  setting it to `None`. A stored `halted_at: null` would come back *marked set* and
  reappear in `to_dict` on the next read, so a resumed or cancelled run would still show
  a review banner for a halt that no longer holds.
- **`record_dropped_columns`** adds `dropped_columns` to the set explicitly, so the key
  is still emitted on a legacy manifest that never carried it.
- **`_always_write_the_store_bookkeeping`** adds `id`, `created_at` and `updated_at`
  back to the set, because `exclude_unset` must never drop the store's own fields.

`to_dict` excludes those same three bookkeeping fields on the way out: a run recorded
none of them, so they are not part of what readers above this module consume.

## Fields that may not be widened

- **`project`** is required. A subset run once defaulted it to `None`; a run that
  cannot name its project has no id to be stored under, and every caller names one.
- **`human_review_queue_stats`** is required with no default. A default would let a
  pre-rename manifest parse silently, hiding queued items from the reader.

## Parameters were flat before they were nested

`parameters: RunParameters` records what the caller asked of this run, verbatim — the
settings a resume replays. Older manifests carried those settings as top-level keys, and
`_LEGACY_PARAMETER_KEYS` maps each `RunParameters` field to the flat key it used to sit
under. That table may only grow.

`_lift_legacy_parameters` **moves** the flat keys rather than copying them: the model
forbids extras, so a payload carrying both spellings would not load at all. A payload
that already has `parameters` is passed through with the flat keys stripped.

## `input_bindings` is a result, not a parameter

It records the preflight provenance of each bound input — absolute path, sha256, and a
byte count streamed at prepare time. It says what the run *found*, not what it was asked
for, which is why it sits beside `parameters` rather than inside it.

## The queue halt's sidecar

`QueueFingerprints` (`app/models/records/queue_fingerprints.py`) is the other record a
run writes about itself: what a `human_review_queue` stage halted on, stored at
`queue_fingerprints/<project>/<run_id>/<stage_id>`.

It never snapshots **columns**. `stage_fingerprint` is one string shared by every pending
row of that halt. `input_fingerprints` and `row_ordinals` hold one entry per row each,
**positionally aligned to the snapshot's row order** — index *n* of one describes the same
row as index *n* of the other.

`row_ordinals` is `None` on a record stored before the runtime recorded them. That is an
unknowable position, and it stays `None` rather than being guessed.

## The human decision itself: a ledger, not a cache entry

A human judgment is not recomputable, so it cannot live only in the stage cache — a
cache exists to replay recomputable work, and deleting a cache row must never destroy
the one thing nothing can regenerate. `app.services.review.record_decision` writes
two things when a reviewer submits a queue card:

- `StageCacheEntry` (`app/core/stage_cache.py`), exactly as for any other stage — the
  replay path a resumed run reads to skip a row a human already decided, and the
  cross-project transport `/admin/cache` copies.
- `ReviewDecision` (`app/models/records/review_decision.py`), an append-only row: verdict,
  the reviewed values, the note, the reviewer, both fingerprints, and the
  `workflow_version` the run was pinned to. Nothing ever edits or deletes a
  `ReviewDecision` — a correction is a new row, keyed the same way, so every past
  judgment stays on record even after a later one supersedes it.

`app.services.review.find_latest_decision` reads the newest row for a match key,
ordered by the record's own `created_at` — never by `reviewed_at`, which a reviewer's
client supplies and this code does not control.
