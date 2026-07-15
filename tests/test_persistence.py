import pytest

from app.core.errors import DocumentNotFound
from app.core.persistence import SqliteKvStore


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
