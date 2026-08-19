"""Chunk position is STORED, never computed from CHUNK_SIZE.

`seq // CHUNK_SIZE` used to locate a chunk, so shrinking the constant made every
chunk written at the old size unreadable. These pin a log written at BOTH sizes,
which is what a run resumed across the change looks like on disk.
"""
from __future__ import annotations

from app.runtime.run_log import (
    CHUNK_SIZE,
    RunEventChunk,
    count_events,
    find_log_end,
    read_events_since,
)
from app.web.run_events import tail_start_seq

PROJECT, RUN = "layout", "r1"
_LEGACY_SIZE = 500          # what every chunk written before first_seq held


def _event(seq: int, stage: str = "s") -> dict:
    return {"seq": seq, "ts": "2026-08-19T10:00:00.000", "kind": "row_ok", "stage": stage}


def _write(index: int, first_seq: int, count: int, *, legacy: bool) -> None:
    """`legacy` writes the record as it was before first_seq existed — the field absent."""
    chunk = RunEventChunk(
        id=RunEventChunk.compose_id(PROJECT, RUN, index),
        events=[_event(first_seq + i) for i in range(count)],
        first_seq=None if legacy else first_seq,
    )
    chunk.save()


def _write_a_resumed_log() -> int:
    """Two full legacy chunks, then chunks at today's size. Returns the event total."""
    _write(0, 0, _LEGACY_SIZE, legacy=True)
    _write(1, _LEGACY_SIZE, _LEGACY_SIZE, legacy=True)
    seq, index = 2 * _LEGACY_SIZE, 2
    for _ in range(3):
        _write(index, seq, CHUNK_SIZE, legacy=False)
        seq += CHUNK_SIZE
        index += 1
    return seq


def test_a_log_written_at_two_sizes_still_counts():
    total = _write_a_resumed_log()
    # Computed from the index this would be `5 * CHUNK_SIZE`, which is the bug.
    assert count_events(PROJECT, RUN) == total


def test_the_writer_resumes_on_the_last_chunk_not_past_it():
    _write_a_resumed_log()
    _, open_index = find_log_end(PROJECT, RUN)
    assert open_index == 4          # the last chunk that exists, so appends land in it


def test_every_event_is_still_reachable_from_any_cursor():
    total = _write_a_resumed_log()
    assert [e["seq"] for e in read_events_since(PROJECT, RUN, 0)] == list(range(total))
    # A cursor inside the legacy region and one inside the new region: the jump
    # is a floor, so both find their chunk rather than landing past it.
    for cursor in (0, 1, _LEGACY_SIZE - 1, _LEGACY_SIZE, total - CHUNK_SIZE, total - 1):
        got = [e["seq"] for e in read_events_since(PROJECT, RUN, cursor)]
        assert got == list(range(cursor, total)), f"cursor {cursor}"


def test_the_tail_is_taken_without_reading_the_whole_log(monkeypatch):
    total = _write_a_resumed_log()
    import app.runtime.run_log as run_log
    loaded: list[int] = []
    real = run_log._load_chunk

    def counting(project_id, run_id, index):
        loaded.append(index)
        return real(project_id, run_id, index)

    monkeypatch.setattr(run_log, "_load_chunk", counting)
    start = tail_start_seq(PROJECT, RUN, tail=CHUNK_SIZE)
    assert start == total - CHUNK_SIZE
    # find_log_end walks the whole log; the tail walk itself must not walk it again.
    assert loaded.count(0) == 1, "chunk 0 was re-read to serve the tail"
