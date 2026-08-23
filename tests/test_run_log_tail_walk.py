"""Opening a log panel reads the newest chunks, not the whole log."""
from __future__ import annotations

from app.runtime.run_log import CHUNK_SIZE, read_events_since
from app.runtime.run_log import RunEventChunk
from app.web.run_events import select_stage_events, tail_start_seq
from run_seed import store_events

PROJECT, RUN, CHUNKS = "tailwalk", "r1", 6


def _seed() -> int:
    """Six full chunks, alternating stage."""
    events = [{"seq": i, "ts": "2026-08-19T10:00:00.000", "kind": "row_ok",
               "stage": "a" if i % 2 else "b", "level": 0}
              for i in range(CHUNKS * CHUNK_SIZE)]
    store_events(PROJECT, RUN, events)
    return len(events)


def _forward_answer(tail: int, stage: str | None) -> int:
    """What the old whole-log read computed."""
    events = select_stage_events(read_events_since(PROJECT, RUN, 0), stage)
    if not events:
        return 0
    if tail <= 0:
        return int(events[-1]["seq"]) + 1
    return 0 if len(events) <= tail else int(events[-tail]["seq"])


def test_it_gives_the_same_answer_as_reading_the_whole_log():
    _seed()
    for stage in (None, "a", "b"):
        for tail in (0, 1, 10, CHUNK_SIZE, CHUNK_SIZE * CHUNKS * 2):
            assert tail_start_seq(PROJECT, RUN, tail, stage) == _forward_answer(tail, stage), (
                f"stage={stage} tail={tail}")


def test_a_short_tail_never_opens_the_oldest_chunks(monkeypatch):
    _seed()
    opened: list[str] = []
    real = RunEventChunk.load_or_none

    def recording(chunk_id):
        opened.append(chunk_id)
        return real(chunk_id)

    monkeypatch.setattr(RunEventChunk, "load_or_none", staticmethod(recording))
    tail_start_seq(PROJECT, RUN, tail=10)
    # The newest chunk holds 500, so ten of them is one open.
    assert len(opened) == 1, opened
    assert opened[0].endswith(f"{CHUNKS - 1:06d}")


def test_a_scoped_tail_keeps_walking_until_it_has_enough(monkeypatch):
    _seed()
    opened: list[str] = []
    real = RunEventChunk.load_or_none
    monkeypatch.setattr(RunEventChunk, "load_or_none",
                        staticmethod(lambda cid: (opened.append(cid), real(cid))[1]))
    # Stage "a" is half the events, so a 600-deep scoped tail spans two chunks.
    tail_start_seq(PROJECT, RUN, tail=600, stage="a")
    assert len(opened) == 3, opened
