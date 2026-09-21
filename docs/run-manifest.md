# The run manifest

`RunManifest` (`app/models/records/run_manifest.py`) is a run's living record, written
by `app/runtime/executor.py` and read by every screen that reports on a run. This page
holds the reasoning its fields depend on, so the record itself stays short enough to
read in one screen.

## The set a run belongs to

A run is either one someone triggered or one an eval executed, and the two are kept
apart so an eval never contaminates what the runs index, the project card or the spend
report count. `RunKind` (`app/models/run_manifest.py`) names the two:

| Member | Value | What it holds |
|---|---|---|
| `RunKind.production` | `runs` | a run someone triggered |
| `RunKind.eval` | `eval_run` | an eval's subset run |

**The set is a FIELD on the record, not a segment of its key.** A manifest is stored at
`run/<project>/<run_id>`, and `kind` is a stored field. The key carries the project
because a project owns its runs; it carries nothing else, so a reader holding a run id
can look the run up without first guessing which set it is in.

**Every reader names the set it wants.** `read_run_manifest(project_id, run_id,
expected_kind)` raises `RunNotFoundError` when the record it finds belongs to another
set — a reader cannot be handed an eval run by accident, and there is no parameter
default that would let a new one silently read production. `resolve_run_dir` takes the
`RunKind` too, and `resolve_kind_dir` (`app/services/workspace.py`) is the only place a
member becomes a directory name.

That last property is what `tests/arch/test_a_run_kind_becomes_a_path_once.py` holds.
It fails on a `RunKind.<member>.value` outside the resolver, and on an `kind` parameter
annotated `str` — the two edits that would let a bare `"runs"` back in. Neither check is by name alone: `"runs"` is also a nav section and a URL segment,
and `kind` also names an event's and a branch's, so the widening check only reads a
module that imports `RunKind`.

`list_run_entries` reads the project off the key and the set off each record, because
that is where each is written down. Filtering both in the store would require the key
and the field to agree about the project, and nothing checks that they do. A torn
payload records no set, so it stays in the listing rather than vanishing from every one.

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

- `StageCacheEntry` (`app/core/stage_cache.py`), exactly as for any other stage — a
  copy computed from the ledger row, and the cross-project transport `/admin/cache` copies.
- `ReviewDecision` (`app/models/records/review_decision.py`), an append-only row: verdict,
  the reviewed values, the note, the reviewer, both fingerprints, and the
  `workflow_version_id` and `workflow_run_id` it was made under. Nothing ever edits or deletes a
  `ReviewDecision` — a correction is a new row, keyed the same way, so every past
  judgment stays on record even after a later one supersedes it.

## A run is handed its decisions; it never fetches them

A run's output has to be explicable from the run itself. A stage that reaches into a
project-scoped store mid-run breaks that: the output then depends on state that is
neither in the run's inputs nor in its record. The row cache is a different case — a
cache hit and a recompute agree, so a run means the same with it or without it. A
decision is an input, not an optimisation.

So `app.services.run.write_run_review_decisions` resolves them BEFORE the run executes,
at prepare and again at resume, and writes them into the run's own directory:

    runs/<run_id>/review_decisions/<stage_id>.parquet

One row per decided row: the decided output row plus an `__input_fingerprint` column to
match on. `_QueueRowMapper` reads that frame, and no module under `app.runtime` imports
`ReviewDecision` or reaches a store for one.

The resolution reads the ledger alone. A cache entry with no ledger row behind it — one
`/admin/cache` imported from another project — still replays, through the row cache,
which is unchanged.

`bust_cache` does not affect this. Busting a cache says "recompute"; a judgement is not
computed, so it is handed over either way and the person who made it is not asked again.

**Where the two disagree, the cache answers first.** `Stage.cache` is `True` for this
type, so the row-driver interceptor replays a cached row before the mapper is called,
and the handed-in frame only answers for rows the cache does not hold.
`record_decision` writes the ledger and the cache together, so only an out-of-band write
reaches a disagreement — see
`tests/test_review_routes.py::test_a_cache_entry_outranks_the_ledger_when_the_two_disagree`.

`app.services.review.find_latest_decision` reads the newest row for a match key,
ordered by the record's own `created_at` — never by `reviewed_at`, which a reviewer's
client supplies and this code does not control.
