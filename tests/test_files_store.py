"""A file's record says where its bytes came from: fetched from an origin, or uploaded."""
from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta

import pytest

from app.core import timestamp_ids
from app.core.errors import MirrorDisagrees
from app.core.files import (
    ProjectFile,
    files_root,
    receive_mirrored_source,
    receive_source,
    resolve_stored_path,
    save_upload,
)
from app.core.persistence import get_store

ORIGIN = "https://example.org/filings/58.pdf"
BODY = b"%PDF-1.4\n%%EOF\n"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.moment = start

    def now(self) -> datetime:
        return self.moment


class _SlowStream(io.BytesIO):
    """Each chunk it hands over moves the clock on, the way a download takes time."""

    def __init__(self, body: bytes, clock: _Clock, per_chunk: timedelta) -> None:
        super().__init__(body)
        self._clock = clock
        self._per_chunk = per_chunk

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(size)
        if chunk:
            self._clock.moment += self._per_chunk
        return chunk


def test_a_record_stored_before_files_had_an_origin_still_loads():
    file_id = "3f1c0d2e9a8b4c7d9e0f1a2b3c4d5e6f"
    # The payload save_upload wrote before the two fields existed, key for key.
    get_store().write(ProjectFile.collection, file_id, {
        "id": file_id, "created_at": "2026-09-01T10:00:00.000000",
        "updated_at": "2026-09-01T10:00:00.000000", "sha256": hashlib.sha256(BODY).hexdigest(),
        "filename": "posts.csv", "byte_count": len(BODY), "project_id": "demo",
        "completeness": "open", "lineage": ""})
    record = ProjectFile.load(file_id)
    assert (record.origin_url, record.fetched_at) == (None, None)


def test_receive_source_records_the_bytes_and_where_they_came_from():
    record = receive_source("demo", ORIGIN, "58.pdf", io.BytesIO(BODY))
    stored = ProjectFile.load(record.id)
    assert stored.sha256 == hashlib.sha256(BODY).hexdigest()
    assert stored.byte_count == len(BODY)
    assert stored.origin_url == ORIGIN
    assert resolve_stored_path(stored).read_bytes() == BODY


def test_fetched_at_is_when_the_read_began_not_when_the_record_was_made(monkeypatch):
    clock = _Clock(datetime(2026, 1, 5, 9, 0, 0))
    monkeypatch.setattr(timestamp_ids, "datetime", clock)
    monkeypatch.setattr(timestamp_ids, "_last_stamp", None)
    stream = _SlowStream(BODY, clock, per_chunk=timedelta(seconds=30))
    record = receive_source("demo", ORIGIN, "58.pdf", stream)
    assert record.fetched_at == "2026-01-05T09:00:00.000000"
    assert record.created_at == "2026-01-05T09:00:30.000000"


def test_an_upload_records_no_origin_and_no_fetch_time():
    record = save_upload("posts.csv", io.BytesIO(b"name,val\nx,1\n"), "demo")
    assert (record.origin_url, record.fetched_at) == (None, None)


@pytest.mark.parametrize("origin", ["javascript:alert(1)", "/Users/someone/58.pdf", "https://"])
def test_an_origin_that_is_not_an_http_url_is_refused_before_a_byte_is_stored(origin):
    with pytest.raises(ValueError, match="must be an http"):
        receive_source("demo", origin, "58.pdf", io.BytesIO(BODY))
    assert ProjectFile.list() == []
    assert not any(files_root().rglob("*"))


def test_a_mirrored_source_keeps_the_time_its_mirror_recorded():
    record = receive_mirrored_source(
        "demo", ORIGIN, "58.pdf", io.BytesIO(BODY), fetched_at="2026-09-28T09:15:16Z",
        expected_sha256=hashlib.sha256(BODY).hexdigest())
    stored = ProjectFile.load(record.id)
    assert stored.origin_url == ORIGIN and stored.fetched_at is not None
    fetched = datetime.fromisoformat(stored.fetched_at)
    assert fetched.tzinfo is None, "stored in now_iso's form: naive local"
    assert fetched.astimezone() == datetime.fromisoformat("2026-09-28T09:15:16Z")
    assert stored.created_at != stored.fetched_at


@pytest.mark.parametrize("fetched_at", ["yesterday", "2026-09-28T09:15:16"])
def test_a_mirrored_source_needs_a_fetch_time_with_an_offset(fetched_at):
    with pytest.raises(ValueError, match="ISO 8601 timestamp with an offset"):
        receive_mirrored_source("demo", ORIGIN, "58.pdf", io.BytesIO(BODY), fetched_at=fetched_at,
                                expected_sha256=hashlib.sha256(BODY).hexdigest())
    assert ProjectFile.list() == []


def test_mirrored_bytes_off_the_recorded_sha256_are_refused_before_a_record_is_made():
    with pytest.raises(MirrorDisagrees, match=f"recorded {'0' * 64} for {ORIGIN}"):
        receive_mirrored_source("demo", ORIGIN, "58.pdf", io.BytesIO(BODY),
                                fetched_at="2026-09-28T09:15:16Z", expected_sha256="0" * 64)
    assert ProjectFile.list() == []
    assert not any(files_root().rglob("*.pdf"))
