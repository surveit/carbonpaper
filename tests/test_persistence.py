from datetime import datetime, timedelta

import pytest

from app.core.errors import DocumentNotFound
from app.core.persistence import PersistedModel, configure_store, validate_id
from app.core.sqlite_store import SqliteKvStore


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


@pytest.mark.parametrize(
    "id_",
    ["abc", "roldugin/20260710T142200", "a/b/c", "a.b-c_d"],
)
def test_validate_id_accepts_safe_ids(id_):
    assert validate_id(id_) == id_


@pytest.mark.parametrize(
    "id_",
    [
        "",
        " x",
        "/abs",
        "a\\b",
        "..",
        "a/../b",
        "a//b",
        "a/",
        "x\x00y",
        "C:/evil",  # POSIX-style drive-absolute id
        "C:\\evil",  # Windows-style drive-absolute id
        "C:evil",  # drive-relative id (no root); anchors to whatever drive is current
    ],
)
def test_validate_id_rejects_unsafe_ids(id_):
    with pytest.raises(ValueError):
        validate_id(id_)


def test_fresh_construct_stamps_created_and_updated_to_now(configured):
    before = datetime.now()
    w = _Widget(id="a", name="hi")
    after = datetime.now()
    created = datetime.fromisoformat(w.created_at)
    updated = datetime.fromisoformat(w.updated_at)
    assert w.created_at == w.updated_at
    assert before - timedelta(seconds=1) <= created <= after + timedelta(seconds=1)
    assert before - timedelta(seconds=1) <= updated <= after + timedelta(seconds=1)


def test_save_advances_updated_at_but_not_created_at(configured):
    # A known past created_at survives a save, while updated_at re-stamps to now
    # on every call — proving the base class, not the caller, owns the stamping.
    past = "2020-01-01T00:00:00"
    w = _Widget(id="a", name="hi", created_at=past, updated_at=past)
    w.save()
    assert w.created_at == past
    assert w.updated_at != past
    reloaded = _Widget.load("a")
    assert reloaded.created_at == past
    assert reloaded.updated_at != past


def test_load_of_a_record_without_timestamps_fills_defaults_via_factory(configured):
    # An OLD stored record written before created_at/updated_at existed carries
    # no such keys. extra="forbid" must still accept it on load: absent field +
    # default_factory means the factory supplies a value rather than raising.
    from app.core.persistence import get_store
    get_store().write("widget", "legacy", {"id": "legacy", "name": "old"})
    w = _Widget.load("legacy")
    assert w.created_at
    assert w.updated_at


def test_persistedmodel_config_mirrors_base():
    # PersistedModel deliberately does not import app.core.models._Base (the
    # store stays free of a models dependency), but its model_config is meant
    # to mirror it exactly. Nothing else enforces that — silent drift here
    # would change on-disk serialization — so pin it down explicitly.
    from app.core.models.schema import _Base
    from app.core.persistence import PersistedModel
    for key in ("extra", "use_enum_values", "validate_default", "populate_by_name"):
        assert PersistedModel.model_config.get(key) == _Base.model_config.get(key)
