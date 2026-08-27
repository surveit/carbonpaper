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

Three modules, not one, sit under that, and two of them are protected:

| Module | Holds | Who may import it |
|---|---|---|
| `app/core/record.py` | `PersistedModel`, `PersistenceScope` | `app.core`, `app.models.records`, `app.runtime` |
| `app/core/persistence.py` | `StoreProtocol`, `get_store`, `configure_store` | `app.core.record`, `app.core.store_config`, `app.core.sqlite_store` |
| `app/core/json_types.py` | `JsonDict`, `JsonScalar` | anyone |

Declaring a record means importing the base, so the first whitelist is the list of
places a stored row's shape may be written down. Holding the handle means being able to
write any collection under any id with no record class in the way, so the second
whitelist closes that off: **under `app/`, a record class is the only way to reach
storage.** Naming the shape of a payload is neither of those acts, which is why the
JSON aliases sit apart and stay open.

Tests are outside both contracts — import-linter's root package is `app` — so a test may
still reach `get_store()` directly to arrange a fixture or assert on the stored bytes.

That door being open is not hypothetical history. `eval` and `eval_run` were stored
collections that no record class described, because `app/evals/store.py` wrote them
through the handle; `run_note` and `archived_run` are rows a rename left behind in the
live store with no class to notice they were orphaned.

### An eval run records no verdict

`EvalRun` carries rollup `metrics` and a `result_ref` pointing at a per-row result
table, and deliberately no overall pass/fail field. An eval-dataset row passes when all
its checks match; whether the eval as a whole *looks good* is a human review judgment,
so storing a bool would be recording a decision nobody made. `status` says only whether
the run finished and how — `running` is the sole non-final value, and a run in flight
exists as a record so it is visible while carrying no metrics, no `result_ref` and no
`finished_at` until the scorer replaces it under the same id.

A record subclass sets `DUMP_OPTS` to any extra `model_dump` kwargs its stored shape
needs — `{"by_alias": True, "exclude_none": True}` for the stage-bearing records above,
`{"exclude_unset": True}` for `RunManifest`. It must never carry `"mode"`, which
`PersistedModel.save` fixes to `"json"`.

Two packages are on that whitelist. `app.models.records` is the home above — every
project record, the runtime's own included. A record the runtime writes is declared
there like any other; its *writer* stays in `app/runtime` and is named in
`app/runtime/_arch_tests/test_stages_no_cross_run_disk.py`, which grants the right to
call `.save()` per writing module rather than per declaration site.

`app.core` is the other, for records that `app/models` sits above in the layers
contract, where a declaration in `app/models` would be unreachable from the module that
needs it: `ProjectFile` (`app/core/files.py`), `StageCacheEntry`
(`app/core/stage_cache.py`), `AgentSession` (`app/core/agent/store.py`) and
`StoredFileShape` (`app/core/file_shape.py`).

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

## `STAGE_SPEC_SCHEMA_VERSION`

`app/models/stage.py` holds one counter for the shape of a stored stage spec: what a
record embedding stages stamps into its `schema_version` column, and what an alembic
revision rewrites a payload up to. `WorkflowVersion`, `WorkingCopy` and `Draft` all
declare it as their `SCHEMA_VERSION`.

| v | what moved | revision |
|---|---|---|
| 2 | `primary_key` left the stage vocabulary; the data model keeps its own | `0002` |
| 3 | `name` became `description` — a stage has one name, its id | `0008` |
| 4 | an input's stored schema left; the graph resolves it (`app.models.workflow`) | `0011` |
| 5 | a report stage stopped storing `template`; the markup lives in `function.code` | `0012` |
| 6 | a union's signature became `extends`, which declares nothing | `0014` |
| 7 | the `publish` type, and its config block, became `report` | `0017` |
| 8 | a workflow output names its `kind`; every stored one is a `figure` | `0018` |

The counter sat at 4 while `0012` stamped 5 and `0014` stamped 6, so a migrated row and
a freshly written one disagreed. It is one counter from 7 on.

The alternative considered and rejected was stamping a newly created store at head so
the replay never happens. Alembic already writes `alembic_version` after its own
upgrade, so the stamp was only ever missing for a store born outside alembic — and
guessing which stores those are has a false positive that skips real migrations and
strands data, where a replay only costs a wasted scan.
