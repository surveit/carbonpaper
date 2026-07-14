# Persistence unification — one dumb store behind `PersistedModel`

**Date:** 2026-07-10
**Status:** approved. Backend decided: document-oriented **SQLite key-value store**
(stdlib `sqlite3`, no ORM). Paths reflect the `app/core.*` + `app/evals.*` reorg (PRs #117/#119): persistence in
`app/core/persistence.py`, evals in `app/evals/`. The §4 foundation is landing; the §5
record-map's per-object locations (`Run` / `Version` / `Compilation` / `AgentSession`)
are re-verified as each subsystem converts.
**North star:** all storage behind one swappable class, so the app speaks objects and
the backend (SQLite today; Postgres or plain files later) changes in exactly one place.

---

## 1. Problem

Saving and loading is done ~nine different ways across 23 files, in three formats
(JSON, YAML, parquet) plus raw bytes/text, over three storage roots:

| Mechanism | Format | Used for |
|---|---|---|
| `model_dump_json(by_alias, exclude_none)` | JSON | compiled stages (`app/services/loader.py`) |
| raw `dict` + `json.dumps(default=str)` | JSON | run manifests, versions, compilations, agent sessions, **`project.json`** |
| `yaml.safe_dump(model_dump())` | **YAML** | eval configs (`eval_config/<id>.yaml`) |
| `model_validate(json.loads())` | JSON | eval runs (`eval_run/<id>.json`) |
| `pandas` → parquet | parquet | run stage-outputs, queue snapshots, row + node decision logs |
| `write_bytes` (immutable) | binary | eval dataset uploads (`eval_data/<file>`) |
| `shutil.copytree` | dirs | version snapshots |
| `glob("*.json")` + scan-by-id | — | finding a stage file |
| raw `.md` / `.txt` | text | methodology prose, compiler errors |

Storage roots: `examples/` (projects, incl. evals), `compilations/` (compiles),
`app/agent/_sessions` (agent/chat). Half of the "documents" are untyped `dict`s
(runs, versions, compilations, sessions, `project.json`), so a shape change corrupts
silently rather than failing validation.

## 2. Goal

One module owns all document storage. Everything above it speaks typed objects and
**never touches storage**. The backend is a document-oriented **SQLite key-value store**
(stdlib `sqlite3`, no ORM) from day one, behind a single interface (`DocumentStore`) so
files-for-inspection or Postgres-later stay one-class drop-ins.

Non-goal (explicitly out of scope, see §10): relational/typed columns for document
bodies (they stay JSON — see §6), any query/index surface beyond CRUD + id-prefix, and
folding human-authored prose (`.md`) into models.

---

## 3. Definitions (cold-read glossary)

- **Document** — one JSON-serializable object identified by `(collection, id)`.
- **Collection** — the "folder" / future table name for one kind of document
  (`project`, `workflow`, `version`, `run`, `compilation`, `agent_session`, `eval`,
  `eval_run`). One string per record type.
- **`id`** — the primary key *within a collection*, a string. For project-scoped
  records it is composite: `"<project>/<local_id>"` (e.g. `roldugin/20260710T142200`).
  In the SQLite store it is just the PK text; `list(prefix="roldugin/")` scopes to one
  project via an indexed `WHERE id LIKE`. No separate "scope" concept lives in the store.
- **Pure contract** — a validation-only Pydantic model in `app/core/models` (`Stage`,
  `TableSchema`, `SchemaLibrary`, `EvalConfig`). Side-effect-free; **never imports
  persistence.**
- **Record** — a `PersistedModel` subclass that *is* a stored document. It embeds
  pure contracts as fields. Records may import persistence; contracts may not.
- **Frame** — a tabular payload read as a `pandas.DataFrame` (run outputs, decision
  logs, and eval-dataset files — all `TableRef`-shaped). Not a document; stored as
  parquet by a parallel `FrameStore` (see §6). Hand-authored prose (`.md`) is a
  non-document too, but stays a source file (§10).

---

## 4. Core design

### 4.1 `DocumentStore` — the storage seam

The entire storage backend is this interface. Nothing outside it (and `FrameStore`)
knows how bytes are stored.

```python
class DocumentStore(Protocol):
    def write(self, collection: str, id: str, data: dict, schema_version: int = 1) -> None: ...
    def read(self, collection: str, id: str) -> dict: ...            # raises DocumentNotFound
    def read_tolerant(self, collection: str, id: str) -> dict | None # None if missing/corrupt
    def delete(self, collection: str, id: str) -> None: ...
    def exists(self, collection: str, id: str) -> bool: ...
    def list_ids(self, collection: str, prefix: str = "") -> list[str]: ...
    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, dict]]: ...
```

- **`SqliteKvStore(db_path)`** (now): one table
  `documents(collection TEXT, id TEXT, data TEXT, schema_version INT, PRIMARY KEY(collection, id))`,
  where `data` is the JSON body. stdlib `sqlite3` only — for an opaque-JSON KV store an
  ORM buys nothing. Writes are atomic and concurrency-safe (WAL mode); `list_ids(prefix)`
  is an indexed `WHERE id LIKE 'prefix%'`. One `db_path` (default `data/app.db`; tests
  pass `":memory:"`) replaces all three current roots.
- The interface keeps other backends a one-class drop-in: a `JsonFileStore` (for
  `cat`/`git diff` inspection) or a `PostgresStore` swaps in via `STORE = …`, with
  nothing above the seam changing.

`list_ids(prefix=...)` is the only nod to scale (an indexed `LIKE`). We add nothing more
speculative — no `query`, no filters into the JSON body — until a real need appears.

**Id path-safety (required).** A composite id embeds a project name that can come from a
model or an upload. The document store keys on it as plain text (no filesystem path), but
`FrameStore` still writes `<collection>/<id>.parquet` files — so ids are validated once,
at the seam: a `/` may nest, but any `..` component or absolute path is rejected. This
centralizes the `is_relative_to` guard currently hand-rolled in `project.py` /
`app/evals/store.py`.

### 4.2 `PersistedModel`

```python
class PersistedModel(_Base):           # _Base = the strict app.core.models base (extra=forbid, ...)
    id: str
    collection: ClassVar[str]          # declared per subclass; the table name
    SCHEMA_VERSION: ClassVar[int] = 1  # bump on a breaking field change; see §6 item 7

    def save(self) -> None:
        STORE.write(self.collection, self.id,
                    self.model_dump(mode="json", **self.DUMP_OPTS),
                    schema_version=self.SCHEMA_VERSION)

    @classmethod
    def load(cls, id: str) -> Self:     # strict: validate-or-raise
        return cls.model_validate(STORE.read(cls.collection, id))

    @classmethod
    def list(cls, prefix: str = "") -> list[Self]:
        return [cls.model_validate(d) for _, d in STORE.read_all(cls.collection, prefix)]
```

`DUMP_OPTS` is a per-model class attribute so a record can preserve exact
serialization (see §7 — the working copy's stages must stay `by_alias, exclude_none`).

Where it lives: `app/core/persistence.py` (shared core — `DocumentStore`, `SqliteKvStore`,
`PersistedModel`, the `STORE` singleton, `DocumentNotFound`). Depends only on stdlib
(`sqlite3`, `json`) + pydantic. `FrameStore` lives beside it (`app/core/frames.py`).

**Where the record classes live:** each in its owning subsystem, importing
`PersistedModel` from `app/core/persistence` — `Run` in `app/runtime`, `AgentSession` in
`app/agent`, `Compilation` in the compiler service (`app/services/compilation.py`),
`Project` / `Workflow` / `Version` in `app/services`, `Eval` / `EvalRun` in
`app/evals/store.py` (which already owns eval persistence). `app/core/models` (the
pure contracts) never imports persistence; the arrow points one way, records →
contracts.

---

## 5. The record map

Every persisted thing becomes exactly one record (or a frame). "Embeds" = held as a
typed field, serialized inline.

| Record | `collection` | `id` | Embeds | Replaces |
|---|---|---|---|---|
| `Project` (identity card) | `project` | `<project>` | title, created_at, model, source | `examples/<p>/project.json` |
| `Workflow` (working copy) | `workflow` | `<project>` | ordered `list[Stage]`, `SchemaLibrary` | `examples/<p>/compiled/NN_*.json`, `schemas/` |
| `Version` | `version` | `<project>/<vid>` | frozen `list[Stage]`, `SchemaLibrary`, meta (parent, message, reviewer, coverage) | `versions/<vid>/{compiled,schemas,version.json}` + **copytree** |
| `Run` | `run` | `<project>/<rid>` | manifest (ordered stage records, status, `workflow_version`, limit/offset overrides) | `runs/<rid>/manifest.json` |
| `Compilation` | `compilation` | `<cid>` (global) | manifest + `what_happened` + **raw** draft stages (`list[dict]`) + `methodology_raw` + `error` | `compilations/<cid>/*` |
| `AgentSession` | `agent_session` | `<sid>` (global) | metadata + jsonable message history | `app/agent/_sessions/<sid>.json` |
| `Eval` | `eval` | `<project>/<eval_id>` | `EvalConfig` | `eval_config/<id>.yaml` — **YAML today → JSON** |
| `EvalRun` | `eval_run` | `<project>/<run_id>` | typed `EvalRun` results | `eval_run/<id>.json` |

Two docs per project (`project` = identity, `workflow` = working copy) mirrors the
current split of `project.json` vs `compiled/` — kept separate so each doc stays small
and single-purpose.

**Frames (parquet, via `FrameStore`) — not documents:**

| Frame | `collection` | `id` |
|---|---|---|
| run stage output | `run_output` | `<project>/<rid>/<stage_id>` |
| review queue snapshot | `run_queue` | `<project>/<rid>/<stage_id>` |
| node decision log | `node_decisions` | `<project>` |
| row decision log | `row_decisions` | `<project>/<stage_id>` |
| eval dataset (uploaded) | `eval_data` | `<project>/<name>` |

`FrameStore` mirrors the doc interface: `save_frame(collection, id, df)` /
`load_frame(collection, id) -> DataFrame` (empty-with-columns when absent). Whole-file
read/modify/write supports today's upsert-by-rewrite in the decision logs unchanged.
An uploaded eval dataset is a `TableRef`-shaped tabular file, read via the same
`read_table` as run outputs — so it is a frame, normalized to parquet on ingest (which
also validates it at upload rather than at first read); write-once for uploads.

---

## 6. Resolved sub-decisions

1. **Working copy is ONE `Workflow` document per project**, not one file per stage.
   Rationale: stage *order* is a property of the ordered collection, not of a `Stage`
   (today it lives in the `NN_` filename prefix). A container doc makes order = list
   order, and deletes the `NN_` prefixes, the `glob`, `find_stage_file`, and
   `copytree` in one move. Editing a node = load `Workflow`, replace one stage, save.
   At prototype scale (tens of stages) rewriting one small JSON file per edit is fine.

2. **Working copy holds only *valid* stages; drafts stay in `Compilation`.** Today the
   compiler writes raw (possibly invalid) dicts straight into the working `compiled/`,
   which is why the viewer needs tolerant loading. Target: a `Compilation` holds raw
   drafts (`list[dict]`); promoting a compile into a project's `Workflow` **validates**
   (raw dict → typed `Stage`). So `Workflow.load` is strict; only `Compilation` display
   needs tolerance, and it already stores raw dicts. `Workflow.load_tolerant(project)`
   still exists for the viewer (validate stage-by-stage, return `(stages, issues)`)
   because a hand-edited doc can still be wrong.

3. **Data model = the `SchemaLibrary` embedded in `Workflow`/`Version`**, not a
   separate collection. Matches node-review, which already treats the whole library as
   one approvable unit.

4. **Evals are already persisted — fold `app/evals/store.py` in, don't greenfield them.**
   Current master persists eval configs as **YAML** (`eval_config/<id>.yaml`, mutable),
   eval runs as JSON (`eval_run/<id>.json`), and dataset uploads as tabular files
   (`eval_data/<file>`). Under the unified layer: `EvalConfig` stays a pure contract in
   `app/core/models`; an `Eval` record embeds it; `EvalRun` becomes a record; the dataset
   **is a frame** (see below), so it goes to `FrameStore`. **Resolved: eval configs move
   YAML → JSON** for uniformity — they are typed models, not hand-authored prose. This
   retires the only YAML in the codebase.

5. **No blob store — eval-dataset uploads are frames.** An uploaded dataset is a
   `TableRef`-shaped tabular file read as a `DataFrame` via the same `read_table` as run
   outputs; it is not opaque. So it lives in `FrameStore` (normalized to parquet on
   ingest), and there is exactly ONE non-document exception — frames — not two. Prose
   (`.md`) stays a source file (§10).

6. **Backend: document-oriented SQLite KV, stdlib `sqlite3`, no ORM.** Considered (a)
   JSON files, (b) SQLite + SQLAlchemy, (c) a normalized/hybrid relational schema. Chose
   SQLite-KV: it gives atomic, concurrency-safe writes and indexed prefix lookups (which
   the runner's mid-run manifest flushes and concurrent agent turns want) without
   pretending to be relational. SQLAlchemy is dropped — for an opaque-JSON KV table it is
   ceremony; the *interface*, not an ORM, is what keeps Postgres a later drop-in.
   Typed/normalized columns were rejected: the models are deeply nested and polymorphic
   (a `Stage` is 1-of-7 shapes) and still churning, so normalizing means ~15–20 tables
   and a migration per model edit. A field can be *promoted* to a real column later if a
   query or FK ever needs one — the JSON body doesn't block that.

7. **Schema evolution: `schema_version` per row + migrate-on-read; reseed for now.** Each
   row stores the writer's `SCHEMA_VERSION`. A breaking field change bumps it and adds a
   tiny `migrate(from_version, data) -> data` run on read, so old rows upgrade lazily and
   data is never lost. Until real (non-example) data exists we simply **reseed** (§9); no
   migrations are written yet. Alembic does not help here — the table shape is fixed; the
   evolving shape lives inside the JSON.

---

## 7. Constraints that must be preserved (correctness, not taste)

- **Stage canonical serialization.** Node-review hashes each stage's
  `model_dump(mode="json", by_alias=True, exclude_none=True)` (alias `schema`, not
  `table_schema`; unset optionals dropped). The `Workflow` record sets `DUMP_OPTS` to
  reproduce this exactly, or every prior approval silently invalidates. The
  loader-injected bookkeeping keys (`_filename/_order/_error`) **disappear** with
  per-file loading, so `CANONICAL_IGNORE_KEYS` shrinks to empty — a simplification, not
  a risk, once nothing injects them.
- **Strict vs tolerant reads.** `load` validates-or-raises (runner, version-create).
  `read_tolerant` / `Workflow.load_tolerant` degrade for the viewer.
- **Fail loudly.** `load` on a missing id raises `DocumentNotFound`; no silent
  empty-object fallback. (Frame logs are the one place an *absent* file legitimately
  means "empty table" — preserved from today.)

---

## 8. What this deletes

- `shutil.copytree` (versions embed their snapshot).
- `NN_` filename ordering, `glob("*.json")`, `find_stage_file`/`write_stage` scan-by-id
  (stage_edit collapses to "mutate the `Workflow` doc, save").
- YAML entirely — the one `yaml.safe_dump`/`safe_load` pair in `app/evals/store.py` (§6.4:
  eval configs are JSON records now).
- Three storage roots → one `data/app.db` SQLite file.
- Every raw `dict` + `json.dumps(default=str)` document (runs, versions, compilations,
  agent sessions, `project.json` via `write_project_meta`) → validated records.
- The bespoke `SessionStore` class and the per-module read/write helpers in
  `loader.py`, `versioning.py`, `compilation.py`, `runner.py`, `loading.py`,
  `node_review.py`, `app/evals/store.py`, `project.py`.

## 9. Migration

Example projects are regenerable prototype data, so **regenerate rather than migrate** in
place. The new store is a single `data/app.db`; the old `examples/`, `compilations/`, and
`_sessions/` layouts are abandoned (reseed = delete the db and re-import). `list_projects()`
becomes `Project.list()` (ids in the `project` collection). No in-place data migration is
written unless the user flags project data on disk they cannot lose.

**Landing on a fast-moving master.** This refactor touches many subsystems while master
advances (100+ commits during design). To keep merge conflicts small: land the additive
`persistence.py` + tests first (conflicts with nothing), then convert one subsystem per
increment, rebasing on master between each. Prefer several small merges over one big one.

## 10. Out of scope

- Other backends (`PostgresStore`, a `JsonFileStore` for inspection) — the interface
  admits them, but only `SqliteKvStore` is built now.
- Any query/index/filter surface beyond `list_ids(prefix)`, and any query into the JSON
  body (promoting a field to a typed column is a later, per-need step — §6 item 6).
- Human-authored prose (`document.md` / `methodology_raw.md`, per-stage source `.md`):
  not models; they stay on disk as source, read as text (like DataFrames, an
  acknowledged non-document). Folding them into records is a separate question. (Eval
  dataset *uploads*, by contrast, ARE brought in — via `FrameStore` — because they are
  tabular data, not hand-authored source.)

## 11. Rollout & testing

- Land `app/core/persistence.py` (`SqliteKvStore` + `PersistedModel`) + `FrameStore` with unit
  tests (round-trip, composite ids, id path-safety, tolerant read, `DocumentNotFound`,
  prefix listing, `schema_version` round-trip) first. Tests run against an in-memory
  `sqlite3` (`":memory:"`), so they stay fast and isolated.
- Convert one subsystem per increment (suggested order: agent sessions → evals →
  compilation → version/run → project/working-copy + viewer), rebasing on master and
  keeping the suite green between each.
- The offline/force-mock test posture and CI (ruff + mypy + pytest) apply; no `Any`,
  no `type: ignore`.