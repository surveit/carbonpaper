import pytest

from app.core.errors import DocumentNotFound
from app.core.persistence import PersistedModel, SqliteKvStore, configure_store


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
    import app.core.persistence as p
    p._store = None
    with pytest.raises(RuntimeError):
        p.get_store()
