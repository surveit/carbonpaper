# Persistence Foundation (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the single document-storage seam — `app/persistence.py` (a SQLite key-value `DocumentStore` + `PersistedModel` base) and `app/frames.py` (`FrameStore` for parquet payloads) — fully tested, consumed by nothing yet.

**Architecture:** One SQLite table `documents(collection, id, data JSON, schema_version)` behind a `DocumentStore` interface; `PersistedModel.save()/load(id)` is the only thing records call. Tabular payloads (frames) get a parallel `FrameStore` over parquet files. Everything is additive — no existing module is modified except adding one exception class — so this phase conflicts with nothing on master and lands first.

**Tech Stack:** Python 3.12, stdlib `sqlite3` + `json`, pydantic v2, pandas/pyarrow (already deps), pytest.

## Global Constraints

Every task's requirements implicitly include these (copied from `docs/persistence-unification.md`):

- **No new runtime dependency.** Store uses stdlib `sqlite3` + `json` only. **Do NOT add SQLAlchemy** — for an opaque-JSON KV table it is ceremony (spec §6 item 6).
- **Document-oriented.** Bodies are JSON; no typed/relational columns for document contents.
- **`no Any`-dodging, no `type: ignore`.** mypy runs over `app/` and must stay clean. The one honest dynamic boundary — an arbitrary JSON body — is the alias `JsonDict = dict[str, Any]`, defined once and used in signatures.
- **ruff clean, incl. BLE:** never `except Exception` / bare `except`. Catch specific types (`sqlite3.Error`, `json.JSONDecodeError`).
- **Exceptions live in `app/errors.py`** (dependency-free), never inline. → `DocumentNotFound` goes there.
- **Fail loudly:** the strict read raises `DocumentNotFound`; no silent empty-object fallback. The tolerant read returns `None`.
- **Composite id convention:** project-scoped ids are `"<project>/<local>"`. Ids that become file paths (FrameStore) are validated — no `..` segment, no absolute path, no backslash.
- **Layering:** `app/persistence.py` depends only on stdlib + pydantic + `app/errors`. It does **not** import `app/models` — so `PersistedModel` defines its own `ConfigDict` (mirroring `app/models/schema.py:_Base`) rather than importing that private base. `app/models` must never import persistence.
- **Tests are offline:** in-memory SQLite (`":memory:"`), no network, no LLM.

**Deviations from the spec sketch (intentional):** `DocumentNotFound` lives in `app/errors.py` (repo convention, not `persistence.py`); `PersistedModel` extends `pydantic.BaseModel` with its own config (not the private `app.models._Base`) to keep the store layer dependency-light per the spec's own "stdlib + pydantic only" rule.

---

## File Structure

- **Create `app/persistence.py`** — `JsonDict`, `validate_id`, `DocumentStore` (Protocol), `SqliteKvStore`, `configure_store`/`get_store`, `PersistedModel`. The whole document seam.
- **Modify `app/errors.py`** — add `DocumentNotFound`.
- **Create `app/frames.py`** — `FrameStore` (parquet payloads, reuses `validate_id`).
- **Create `tests/test_persistence.py`** — store CRUD, prefix scoping, schema_version, id-safety, `PersistedModel`.
- **Create `tests/test_frames.py`** — `FrameStore` round-trip, write-once, path-safety.

---

### Task 1: `validate_id` + module skeleton

**Files:**
- Create: `app/persistence.py`
- Test: `tests/test_persistence.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `JsonDict = dict[str, Any]`; `validate_id(id: str) -> str` (returns the id if safe, else raises `ValueError`). Rejects empty/untrimmed, leading `/`, backslash, NUL, and any `""`/`".."` segment when split on `/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_persistence.py
import pytest

from app.persistence import validate_id


@pytest.mark.parametrize("good", ["abc", "roldugin/20260710T142200", "a/b/c", "a.b-c_d"])
def test_validate_id_accepts_safe(good):
    assert validate_id(good) == good


@pytest.mark.parametrize("bad", ["", " x", "/abs", "a\\b", "..", "a/../b", "a//b", "a/", "x\x00y"])
def test_validate_id_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        validate_id(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.persistence'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/persistence.py
"""The single document-storage seam. Everything above speaks typed PersistedModel
objects; only this module knows they live in a SQLite key-value table.

One table, documents(collection, id, data, schema_version), where `data` is the
JSON body of one pydantic record. Swapping the backend (Postgres, or plain files
for inspection) is a new DocumentStore implementation + one configure_store call;
nothing above the seam changes. See docs/persistence-unification.md.
"""
from __future__ import annotations

from typing import Any

# The one honest dynamic boundary: an arbitrary pydantic model_dump / JSON body.
JsonDict = dict[str, Any]


def validate_id(id: str) -> str:
    """Return `id` if it is safe to use as a storage key and relative path
    component, else raise ValueError. A composite id (`<project>/<local>`) may
    contain `/`, but never an empty or `..` segment, a leading `/`, a backslash,
    or a NUL — so an id sourced from a model or an upload can't escape its
    collection when a backend (FrameStore) turns it into a file path."""
    if not id or id != id.strip():
        raise ValueError(f"empty or untrimmed id: {id!r}")
    if id.startswith("/") or "\\" in id or "\x00" in id:
        raise ValueError(f"unsafe id: {id!r}")
    if any(part in ("", "..") for part in id.split("/")):
        raise ValueError(f"unsafe id (empty or '..' segment): {id!r}")
    return id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: PASS (both parametrized tests).

- [ ] **Step 5: Commit**

```bash
git add app/persistence.py tests/test_persistence.py
git commit -m "$(printf 'feat(persistence): add validate_id + module skeleton\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: `DocumentNotFound` + `SqliteKvStore` core (write / read / schema_version)

**Files:**
- Modify: `app/errors.py`
- Modify: `app/persistence.py`
- Test: `tests/test_persistence.py`

**Interfaces:**
- Consumes: `JsonDict` (Task 1).
- Produces:
  - `app.errors.DocumentNotFound(Exception)`.
  - `SqliteKvStore(db_path: str)` with `write(collection: str, id: str, data: JsonDict, schema_version: int = 1) -> None`, `read(collection: str, id: str) -> JsonDict` (raises `DocumentNotFound` on miss), `schema_version(collection: str, id: str) -> int`. Backed by a `documents(collection, id, data, schema_version, PRIMARY KEY(collection, id))` table in WAL mode.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_persistence.py  (append)
from app.errors import DocumentNotFound
from app.persistence import SqliteKvStore


@pytest.fixture
def store():
    return SqliteKvStore(":memory:")


def test_write_then_read_roundtrips(store):
    store.write("run", "proj/1", {"status": "ok", "rows": 3})
    assert store.read("run", "proj/1") == {"status": "ok", "rows": 3}


def test_write_is_upsert(store):
    store.write("run", "proj/1", {"status": "running"})
    store.write("run", "proj/1", {"status": "ok"})
    assert store.read("run", "proj/1") == {"status": "ok"}


def test_read_missing_raises_document_not_found(store):
    with pytest.raises(DocumentNotFound):
        store.read("run", "proj/nope")


def test_schema_version_persisted(store):
    store.write("run", "proj/1", {"status": "ok"}, schema_version=4)
    assert store.schema_version("run", "proj/1") == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistence.py -k "roundtrips or upsert or not_found or schema_version" -v`
Expected: FAIL — `ImportError: cannot import name 'SqliteKvStore'` (and `DocumentNotFound`).

- [ ] **Step 3: Write minimal implementation**

Add to `app/errors.py`:

```python
class DocumentNotFound(Exception):
    """No document exists for a (collection, id) in the store. Raised by the
    strict read path (DocumentStore.read / PersistedModel.load); the tolerant
    path returns None instead. A genuine miss surfaced loudly, never a fabricated
    empty document."""
```

Add to `app/persistence.py` (imports at top, class below `validate_id`):

```python
import json
import sqlite3

from app.errors import DocumentNotFound


class SqliteKvStore:
    """DocumentStore backed by one SQLite table: opaque JSON bodies keyed by
    (collection, id). Writes are atomic; WAL mode lets readers run concurrently
    with a writer. `db_path` is a file path or ":memory:" (tests)."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            "  collection TEXT NOT NULL,"
            "  id TEXT NOT NULL,"
            "  data TEXT NOT NULL,"
            "  schema_version INTEGER NOT NULL DEFAULT 1,"
            "  PRIMARY KEY (collection, id))"
        )
        self._conn.commit()

    def write(self, collection: str, id: str, data: JsonDict, schema_version: int = 1) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
            "VALUES (?, ?, ?, ?)",
            (collection, id, json.dumps(data), schema_version),
        )
        self._conn.commit()

    def read(self, collection: str, id: str) -> JsonDict:
        row = self._conn.execute(
            "SELECT data FROM documents WHERE collection=? AND id=?", (collection, id)
        ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        parsed: JsonDict = json.loads(row[0])
        return parsed

    def schema_version(self, collection: str, id: str) -> int:
        row = self._conn.execute(
            "SELECT schema_version FROM documents WHERE collection=? AND id=?",
            (collection, id),
        ).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{id}")
        return int(row[0])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_persistence.py -k "roundtrips or upsert or not_found or schema_version" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/errors.py app/persistence.py tests/test_persistence.py
git commit -m "$(printf 'feat(persistence): SqliteKvStore write/read + DocumentNotFound\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: `SqliteKvStore` — `exists`, `delete`, `read_tolerant`

**Files:**
- Modify: `app/persistence.py`
- Test: `tests/test_persistence.py`

**Interfaces:**
- Consumes: `SqliteKvStore` (Task 2).
- Produces: `exists(collection, id) -> bool`, `delete(collection, id) -> None` (no error if absent), `read_tolerant(collection, id) -> JsonDict | None` (None if missing **or** the stored body is unparseable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_persistence.py  (append)
def test_exists_and_delete(store):
    assert store.exists("run", "proj/1") is False
    store.write("run", "proj/1", {"x": 1})
    assert store.exists("run", "proj/1") is True
    store.delete("run", "proj/1")
    assert store.exists("run", "proj/1") is False


def test_delete_missing_is_silent(store):
    store.delete("run", "proj/absent")  # no raise


def test_read_tolerant_missing_returns_none(store):
    assert store.read_tolerant("run", "proj/absent") is None


def test_read_tolerant_corrupt_returns_none(store):
    # White-box: write a non-JSON body straight past write()'s json.dumps.
    store._conn.execute(
        "INSERT INTO documents (collection, id, data) VALUES (?, ?, ?)",
        ("run", "proj/bad", "{not json"),
    )
    store._conn.commit()
    assert store.read_tolerant("run", "proj/bad") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistence.py -k "exists_and_delete or delete_missing or read_tolerant" -v`
Expected: FAIL — `AttributeError: 'SqliteKvStore' object has no attribute 'exists'`.

- [ ] **Step 3: Write minimal implementation**

Add to `SqliteKvStore`:

```python
    def exists(self, collection: str, id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM documents WHERE collection=? AND id=?", (collection, id)
        ).fetchone()
        return row is not None

    def delete(self, collection: str, id: str) -> None:
        self._conn.execute(
            "DELETE FROM documents WHERE collection=? AND id=?", (collection, id)
        )
        self._conn.commit()

    def read_tolerant(self, collection: str, id: str) -> JsonDict | None:
        row = self._conn.execute(
            "SELECT data FROM documents WHERE collection=? AND id=?", (collection, id)
        ).fetchone()
        if row is None:
            return None
        try:
            parsed: JsonDict = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return parsed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_persistence.py -k "exists_and_delete or delete_missing or read_tolerant" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/persistence.py tests/test_persistence.py
git commit -m "$(printf 'feat(persistence): exists/delete/read_tolerant\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 4: `SqliteKvStore` — `list_ids` / `read_all` with prefix scoping

**Files:**
- Modify: `app/persistence.py`
- Test: `tests/test_persistence.py`

**Interfaces:**
- Consumes: `SqliteKvStore` (Task 2).
- Produces: `list_ids(collection, prefix="") -> list[str]` (sorted) and `read_all(collection, prefix="") -> Iterator[tuple[str, JsonDict]]`. `prefix` is an indexed range scan (`id >= prefix AND id < prefix++`), so `list_ids("run", "roldugin/")` returns only that project's runs.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_persistence.py  (append)
def test_list_ids_scopes_by_prefix(store):
    store.write("run", "roldugin/1", {"n": 1})
    store.write("run", "roldugin/2", {"n": 2})
    store.write("run", "assad/1", {"n": 9})
    assert store.list_ids("run") == ["assad/1", "roldugin/1", "roldugin/2"]
    assert store.list_ids("run", "roldugin/") == ["roldugin/1", "roldugin/2"]


def test_read_all_yields_id_and_body(store):
    store.write("run", "p/1", {"n": 1})
    store.write("run", "p/2", {"n": 2})
    assert dict(store.read_all("run", "p/")) == {"p/1": {"n": 1}, "p/2": {"n": 2}}


def test_list_ids_empty_collection(store):
    assert store.list_ids("nothing") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistence.py -k "list_ids or read_all" -v`
Expected: FAIL — `AttributeError: 'SqliteKvStore' object has no attribute 'list_ids'`.

- [ ] **Step 3: Write minimal implementation**

Add `from typing import Iterator` to the imports, then add to `SqliteKvStore`:

```python
    def _scan(self, columns: str, collection: str, prefix: str) -> sqlite3.Cursor:
        # `columns` is an internal literal, never user input. Prefix match is an
        # index-friendly range on the (collection, id) primary key.
        if prefix:
            hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            return self._conn.execute(
                f"SELECT {columns} FROM documents "
                "WHERE collection=? AND id>=? AND id<? ORDER BY id",
                (collection, prefix, hi),
            )
        return self._conn.execute(
            f"SELECT {columns} FROM documents WHERE collection=? ORDER BY id",
            (collection,),
        )

    def list_ids(self, collection: str, prefix: str = "") -> list[str]:
        return [row[0] for row in self._scan("id", collection, prefix)]

    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]:
        for row_id, data in self._scan("id, data", collection, prefix):
            body: JsonDict = json.loads(data)
            yield row_id, body
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_persistence.py -k "list_ids or read_all" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/persistence.py tests/test_persistence.py
git commit -m "$(printf 'feat(persistence): list_ids/read_all with prefix scoping\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 5: `DocumentStore` protocol + `PersistedModel` + `configure_store`

**Files:**
- Modify: `app/persistence.py`
- Test: `tests/test_persistence.py`

**Interfaces:**
- Consumes: `SqliteKvStore` (Tasks 2–4).
- Produces:
  - `DocumentStore(Protocol)` — the interface `SqliteKvStore` satisfies (`write`/`read`/`read_tolerant`/`exists`/`delete`/`list_ids`/`read_all`).
  - `configure_store(store: DocumentStore) -> None`, `get_store() -> DocumentStore` (raises if unconfigured).
  - `PersistedModel(BaseModel)` with class attrs `collection: ClassVar[str]`, `SCHEMA_VERSION: ClassVar[int] = 1`, `DUMP_OPTS: ClassVar[JsonDict] = {}`, field `id: str`, and methods `save()`, `load(id) -> Self`, `load_or_none(id) -> Self | None`, `list(prefix="") -> list[Self]`, `delete(id)`, `exists(id) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_persistence.py  (append)
from app.persistence import PersistedModel, configure_store


class _Widget(PersistedModel):
    collection = "widget"
    name: str
    count: int = 0


@pytest.fixture
def configured():
    configure_store(SqliteKvStore(":memory:"))


def test_save_and_load(configured):
    _Widget(id="a", name="hi", count=2).save()
    got = _Widget.load("a")
    assert (got.name, got.count) == ("hi", 2)


def test_load_or_none_missing(configured):
    assert _Widget.load_or_none("absent") is None


def test_list_returns_all_typed(configured):
    _Widget(id="a", name="x").save()
    _Widget(id="b", name="y").save()
    names = sorted(w.name for w in _Widget.list())
    assert names == ["x", "y"]


def test_delete_and_exists(configured):
    _Widget(id="a", name="x").save()
    assert _Widget.exists("a") is True
    _Widget.delete("a")
    assert _Widget.exists("a") is False


def test_get_store_unconfigured_raises():
    import app.persistence as p
    p._store = None
    with pytest.raises(RuntimeError):
        p.get_store()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistence.py -k "save_and_load or load_or_none or list_returns or delete_and_exists or unconfigured" -v`
Expected: FAIL — `ImportError: cannot import name 'PersistedModel'`.

- [ ] **Step 3: Write minimal implementation**

Extend the imports and add to `app/persistence.py`:

```python
from typing import ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict


class DocumentStore(Protocol):
    def write(self, collection: str, id: str, data: JsonDict, schema_version: int = 1) -> None: ...
    def read(self, collection: str, id: str) -> JsonDict: ...
    def read_tolerant(self, collection: str, id: str) -> JsonDict | None: ...
    def exists(self, collection: str, id: str) -> bool: ...
    def delete(self, collection: str, id: str) -> None: ...
    def list_ids(self, collection: str, prefix: str = "") -> list[str]: ...
    def read_all(self, collection: str, prefix: str = "") -> Iterator[tuple[str, JsonDict]]: ...


_store: DocumentStore | None = None


def configure_store(store: DocumentStore) -> None:
    """Install the process-wide document store. App startup calls this once with a
    SqliteKvStore('data/app.db'); each test installs a fresh SqliteKvStore(':memory:')."""
    global _store
    _store = store


def get_store() -> DocumentStore:
    if _store is None:
        raise RuntimeError("document store not configured; call configure_store() first")
    return _store


class PersistedModel(BaseModel):
    """Base for every stored record. A subclass sets `collection` (the table name)
    and carries an `id` (its primary key); save()/load()/list() go through the
    configured DocumentStore, so nothing above this class touches storage. The
    body is serialized as JSON (see docs/persistence-unification.md).

    Its own strict config mirrors app.models._Base without importing it, so the
    storage layer stays free of an app.models dependency."""

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
        populate_by_name=True,
    )

    id: str
    collection: ClassVar[str]
    SCHEMA_VERSION: ClassVar[int] = 1
    # Extra model_dump kwargs a subclass needs to preserve exact on-disk shape
    # (e.g. {"by_alias": True, "exclude_none": True} for a stage-bearing record).
    # Must not include "mode" — that is fixed to "json".
    DUMP_OPTS: ClassVar[JsonDict] = {}

    def save(self) -> None:
        get_store().write(
            self.collection,
            self.id,
            self.model_dump(mode="json", **self.DUMP_OPTS),
            schema_version=self.SCHEMA_VERSION,
        )

    @classmethod
    def load(cls, id: str) -> Self:
        return cls.model_validate(get_store().read(cls.collection, id))

    @classmethod
    def load_or_none(cls, id: str) -> Self | None:
        data = get_store().read_tolerant(cls.collection, id)
        return cls.model_validate(data) if data is not None else None

    @classmethod
    def list(cls, prefix: str = "") -> list[Self]:
        return [cls.model_validate(data)
                for _, data in get_store().read_all(cls.collection, prefix)]

    @classmethod
    def delete(cls, id: str) -> None:
        get_store().delete(cls.collection, id)

    @classmethod
    def exists(cls, id: str) -> bool:
        return get_store().exists(cls.collection, id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: PASS (all persistence tests, ~18).

- [ ] **Step 5: Commit**

```bash
git add app/persistence.py tests/test_persistence.py
git commit -m "$(printf 'feat(persistence): PersistedModel + DocumentStore protocol + configure_store\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 6: `FrameStore` (parquet payloads)

**Files:**
- Create: `app/frames.py`
- Test: `tests/test_frames.py`

**Interfaces:**
- Consumes: `validate_id` (Task 1).
- Produces: `FrameStore(root: Path)` with `save_frame(collection, id, frame, *, overwrite=True) -> None` (raises `FileExistsError` when `overwrite=False` and present), `load_frame(collection, id) -> pd.DataFrame | None` (None if absent), `exists(collection, id) -> bool`, `delete(collection, id) -> None`. Files land at `<root>/<collection>/<id>.parquet`; a composite id creates subdirectories; an unsafe id raises `ValueError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_frames.py
import pandas as pd
import pytest

from app.frames import FrameStore


@pytest.fixture
def frames(tmp_path):
    return FrameStore(tmp_path)


def test_save_then_load_roundtrips(frames):
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    frames.save_frame("run_output", "proj/run1/stageA", df)
    loaded = frames.load_frame("run_output", "proj/run1/stageA")
    pd.testing.assert_frame_equal(loaded, df)


def test_load_missing_returns_none(frames):
    assert frames.load_frame("run_output", "proj/absent") is None


def test_write_once_refuses_overwrite(frames):
    df = pd.DataFrame({"a": [1]})
    frames.save_frame("eval_data", "proj/set1", df, overwrite=False)
    with pytest.raises(FileExistsError):
        frames.save_frame("eval_data", "proj/set1", df, overwrite=False)


def test_unsafe_id_rejected(frames):
    with pytest.raises(ValueError):
        frames.save_frame("run_output", "../escape", pd.DataFrame({"a": [1]}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_frames.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.frames'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/frames.py
"""Parquet storage for the tabular payloads that aren't documents — run stage
outputs, review-queue snapshots, decision logs, and uploaded eval datasets. Same
(collection, id) addressing as the document store, different physical form: one
parquet file per frame under a root directory. The only place outside the
document store that turns an id into a file path, so it reuses validate_id."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.persistence import validate_id


class FrameStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, collection: str, id: str) -> Path:
        validate_id(id)
        return self.root / collection / f"{id}.parquet"

    def save_frame(
        self, collection: str, id: str, frame: pd.DataFrame, *, overwrite: bool = True
    ) -> None:
        path = self._path(collection, id)
        if path.exists() and not overwrite:
            raise FileExistsError(f"frame already exists: {collection}/{id}")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)

    def load_frame(self, collection: str, id: str) -> pd.DataFrame | None:
        path = self._path(collection, id)
        if not path.exists():
            return None
        return pd.read_parquet(path)

    def exists(self, collection: str, id: str) -> bool:
        return self._path(collection, id).exists()

    def delete(self, collection: str, id: str) -> None:
        self._path(collection, id).unlink(missing_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_frames.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/frames.py tests/test_frames.py
git commit -m "$(printf 'feat(persistence): FrameStore for parquet payloads\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Final verification (after Task 6)

- [ ] **Full suite green:** `python -m pytest tests/test_persistence.py tests/test_frames.py -v` → all pass.
- [ ] **No regressions:** `python -m pytest -q` → the whole suite still passes (this phase adds files, modifies only `app/errors.py`).
- [ ] **Types clean:** `python -m mypy app/persistence.py app/frames.py app/errors.py` → no errors (no `Any`-dodge, no `type: ignore`).
- [ ] **Lint clean:** `python -m ruff check app/persistence.py app/frames.py app/errors.py tests/test_persistence.py tests/test_frames.py` → no findings (no blind except).

## What Phase 1 deliberately does NOT do

Nothing consumes the store yet — no subsystem is converted. Those are follow-on plans (one per subsystem, each its own small PR, rebased on master), in the spec's §11 order: **agent sessions → evals → compilation → version/run → project/working-copy + viewer.** Each converts one subsystem's read/write sites to a `PersistedModel` (or `FrameStore`) and deletes the bespoke helper it replaces.
