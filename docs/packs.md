# Packs

A **pack** is a subpackage `app/packs/<pack_id>/` that registers connector kinds. Every other
package under `app/` is the kernel.

## What a pack declares

Its `__init__.py` calls `register_pack(PackSpec(...))` (`app/models/packs.py`). A `PackSpec`
holds a `pack_id` and its `connectors`, each a `ConnectorSpec` (`app/models/connectors.py`):

- `kind`: what an `input_data` stage's `connector.kind` names.
- `params_model`: a `ConnectorParams` subclass, so every kind binds `paths` the same way.
- `metadata_columns`: the source table's columns after `SOURCE_COLUMNS` (`source_id`,
  `source_sha256`, `filename`, `origin_url`, `fetched_at`).
- `acquire(params)`: yields an `AcquiredBytes` per file: filename, origin URL, a way to open
  the bytes, metadata. A mirror's copy is a `MirroredBytes`, adding the time and sha256 the
  mirror recorded.

At import, `register_pack` refuses a pack id or kind already held, `file` included, and a
metadata column that is not nullable, starts with `_`, repeats a source column or is
declared twice.

A pack imports only `app.models`, `app.core` and itself (`app/packs/_arch_tests/`), and
writes no file or record: the kernel stores the bytes it hands over.

## How a connector is bound at run time

A run's bindings merge into `connector.params` key by key (`Workflow.apply_run_bindings`),
and the kind's `params_model` validates the result. Once every other check has passed,
`prepare_run` calls `acquire_input_data` for each stage of a pack's kind:

1. **Bound `paths` win.** Each must be a stored file of the project. Its metadata is null,
   which is why every metadata column must be nullable.
2. **Otherwise `acquire(params)` runs.** `receive_source` stores each file and stamps when
   its read began; `receive_mirrored_source` keeps the mirror's time, refusing bytes that
   hash to anything else.
3. Each file becomes a `ReadFile` in the manifest's `input_bindings`.
4. Only once every stage has acquired does each write its source table, one row per file,
   to `runs/<id>/sources/<stage_id>.parquet`, so a refused run leaves no run directory. The
   handler reads that table.

A file the connector cannot reach raises `SourceUnavailable`, refusing the run with the
stage and file named. A resume reads the table its prepare wrote. A workflow test or an eval
prepares no run, so a stage of a pack's kind fails there.

## The docket pack

`recap_docket` reads a CourtListener docket's filings, one row per PDF; `entries` names each
by ECF number (`"58"`, `"221-1"`). It reads the docket's `type=rd` search pages and each PDF on
storage.courtlistener.com; a `cache_dir` whose `manifest.jsonl` records each fetch replaces
both. A filing the archive or mirror lacks refuses the run. `ATTRIBUTION.md` holds the terms
line.
