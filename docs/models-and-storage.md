# Data model (Pydantic) + storage convention

## Data model — `app/models/` — IMPLEMENTED

The workflow contract is a Pydantic package: `Stage` (a discriminated union over one
model per stage type — parse with `parse_stage`), `Workflow`, and the config blocks
(`Connector`, `LLMConfig`, `PythonFunction`, `JoinConfig`,
`AggregateConfig`, …) plus `Column` / `TableSchema`. **Constructing a model
validates it** — the model *is* the contract, so there's no separate validator to
keep in sync.

This **replaces and removes** two older things:
- `app/schema.py` — a dataclass *spec* that was imported by nothing.
- `app/dag_schema.py` — hand-rolled validators returning issue-string lists.

Convenience entry points remain for the non-fatal, "show the user the problems"
case: `validate_workflow(stages) -> list[str]` and `validate_stage(stage) -> list[str]`
(empty list = valid). `parse_workflow(stages) -> Workflow` raises instead.

**Cut in this change (per review):**
- Connector kinds reduced to the implemented `file`. The rest
  (`http`/`scrape`/`api`/`manual_upload`/`sql`) were declared but never had a
  handler — add them back alongside a handler.
- Weighted aggregation formulas (`weighted_mean`/`weighted_sum`) — unused in the
  compiled workflows (weighting is done inside `python_frame_function` modules).

**Enforced at load, via `app/services/loader.py`.** This is the only place that
reads a project's `working_copy` document (a list of stage specs, each the JSON
dump of a validated `Stage`, in the order the UI shows them); everything past it
speaks `Stage` objects, not dicts. Two entry points, both parsing each spec
through `parse_stage`:
- `load_workflow` — strict, for the runner. Any invalid stage or
  cross-stage issue raises `WorkflowLoadError`, and the runner refuses to
  execute the workflow.
- `load_stage_entries` — tolerant, per-stage, for the viewer. Each spec gets a
  `StageEntry` (parsed `Stage` or `None` + an issues list). If any stage is
  invalid, the viewer surfaces the issues and renders no workflow at all
  (a partial graph with holes would mislead) instead of crashing.

`app/runtime/handlers.py`, `runner.py`, `preview.py`, and the web layer all consume
the typed `Stage` objects this loader returns.

## Storage — two layers, and nothing else

A project's state lives in exactly two places:

- **The document store** (`app/core/persistence.py`), a SQLite key-value table
  keyed `(collection, id)`. Every stored record is a `PersistedModel`: the
  methodology, the working copy, each `workflow_version`, a run's record and its
  chunked event log, the review-queue fingerprints, the review decisions, the
  terms, and the uploaded-file index.
- **Frames** (`app/core/frames.py`), the parquet files a run reads and writes.

`tests/arch/test_persistence_is_frames_and_the_store.py` holds this: nothing under
`app/` writes a file except frames, an export the user downloads, and a file the
user uploaded. What is left on disk under a project is `code/`, `data/` and
`runs/<id>/{outputs, artifacts, queue}` — frames and the files around them.

### Where a record is declared

A record a project owns is declared in `app/models/records/`, one module per record,
and nothing but the declaration lives there: the functions that load, mutate and save
it stay in the service that owns its lifecycle. `app/services/project.py` still holds
project creation, deletion and metadata; the `Project` class it operates on is
`app/models/records/project.py`.

Two modules, not one, sit under that:

- `app/core/record.py` — `PersistedModel` and `PersistenceScope`. Declaring a record
  means importing this, so the import-linter contract protecting it (`pyproject.toml`)
  is the list of places a stored row's shape may be written down.
- `app/core/persistence.py` — the store seam: the `DocumentStore` protocol, the
  process-wide handle, `now_iso`, and the JSON aliases. Unprotected, because reaching
  storage is not the same act as declaring a row, and a service legitimately does the
  first without doing the second.

A record subclass sets `DUMP_OPTS` to any extra `model_dump` kwargs its stored shape
needs — `{"by_alias": True, "exclude_none": True}` for the stage-bearing records above,
`{"exclude_unset": True}` for `RunManifest`. It must never carry `"mode"`, which
`PersistedModel.save` fixes to `"json"`.

Three packages are on that whitelist. `app.models.records` is the home above.
`app.runtime` keeps its own four — `RunManifest`, `RunEventChunk`, `StageCitations`,
`QueueFingerprints` — because
`app/runtime/_arch_tests/test_stages_no_cross_run_disk.py` grants a runtime module the
right to call `.save()` only while that module *declares* a `PersistenceScope.RUN`
record; moving the declaration out would revoke the write. `app.core` keeps three
records that `app/models` sits above in the layers contract, so a declaration in
`app/models` would be unreachable from the module that needs it: `ProjectFile`
(`app/core/files.py`), `StageCacheEntry` (`app/core/stage_cache.py`) and `AgentSession`
(`app/core/agent/store.py`).

### The stage spec-dict shape

`WorkflowVersion`, `WorkingCopy` and `Draft` each embed a list of `Stage` objects and
each sets `DUMP_OPTS = {"by_alias": True, "exclude_none": True}`. That is the *spec-dict
shape* — field aliases restored, unset optionals dropped — which is what
`stage_to_spec_dict` produces. All three share it deliberately: a stage must read
identically in the working copy, in a draft, and in a version cut from either, so an
edit that changes nothing produces no diff in the stored document.

### An optional field may be load-bearing

`PersistedModel.load` validates with `extra="forbid"` and no tolerance for a missing
required key, so widening a stored field is cheap and narrowing one is not. `Project.name`
is the standing example: projects created before labels existed carry no `name` key at
all, and making the field required would fail to load every one of them. `None` there is
not a missing label — it means the project id is still the only name it has, which
`Project.label()` reports.

## Migrations replay, so every revision must be a no-op at head

`./start` runs `alembic upgrade head` on boot. A store created by
`configure_default_document_store` — the CLI's, the MCP server's, a test's — carries no
`alembic_version` row, because nothing but alembic writes one. Alembic therefore reads
it as being at the baseline and replays `0001` onward over data current code already
wrote.

That replay is the normal path, not an edge case, so the invariant every revision owes
is: **running it over a store already at head changes nothing.** A revision earns that
by recognising a record whose new shape is already present and skipping it — not
rewriting it, since a rewrite also re-stamps `schema_version` and walks a record
backwards to the version that revision wrote.

`tests/test_migration_replay.py` holds it. The store it upgrades is seeded through the
same service calls the app uses — `create_project`, `add_stages`,
`save_working_copy_as_version`, `save_upload` — so the documents under test are whatever
today's models write, and a model change moves the fixture with it. After
`upgrade head` every document must be byte-identical, `schema_version` included, the
uploaded bytes must be where they were, and `alembic_version` must read head.

The alternative considered and rejected was stamping a newly created store at head so
the replay never happens. Alembic already writes `alembic_version` after its own
upgrade, so the stamp was only ever missing for a store born outside alembic — and
guessing which stores those are has a false positive that skips real migrations and
strands data, where a replay only costs a wasted scan.
