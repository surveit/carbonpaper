"""Per-run event log: the live-tailed drill-down companion to the run manifest.

Workers emit lock-free; one writer thread appends to fixed-size stored chunks,
which are both the SSE feed and the historical record. The manifest stays the
source of truth for stage status.
"""
from __future__ import annotations

import contextvars
import queue
import threading
from datetime import datetime
from typing import Any, ClassVar, Iterator

from app.core.persistence import JsonDict, PersistedModel, PersistenceScope

# Sentinel pushed by close() to tell the writer thread to drain and stop.
_STOP = object()

# Events per stored chunk: enough that a 270k-event run is documents in the
# thousands rather than 270k, small enough that rewriting the open one on each
# flush stays cheap. Free to change — a chunk records where it starts, so no
# reader computes position from this number.
CHUNK_SIZE = 100

# The most events any chunk has ever held. read_events_since needs a starting
# index WITHOUT loading chunks to look at their first_seq, and `from_seq // this`
# is that index: it lands at or before the chunk that holds from_seq whatever mix
# of sizes a run was written in. NEVER lower it below a value CHUNK_SIZE has
# already been in production, or the jump overshoots and a reader silently skips
# the events it was asked for.
_WIDEST_CHUNK_SIZE = 500

# How long the writer thread waits for more events before flushing what it has,
# so a slow run's SSE feed does not stall behind an unfilled chunk. It also sets
# the write cost, because every flush REWRITES the open chunk whole: this is how
# many times one document is re-serialised on its way to full. Measured over a
# real 100k-event run, 17.2% of inter-event gaps exceed 0.25s and 0.7% exceed
# 1.0s — 3.3GB of store traffic against 160MB for the same log. The SSE feed
# polls at 0.5s, so the wider window costs at most a second of lag in the panel.
_FLUSH_INTERVAL_S = 1.0


class RunEventChunk(PersistedModel):
    """One run's events `first_seq .. first_seq + len(events) - 1`."""

    collection: ClassVar[str] = "run_events"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    events: list[JsonDict] = []
    # Required, with no default: alembic 0013 backfilled every chunk written
    # before the field, so one arriving without it is a chunk this process should
    # refuse rather than place at seq 0 and serve the wrong events for.
    first_seq: int

    @staticmethod
    def compose_id(project_id: str, run_id: str, index: int) -> str:
        return f"{project_id}/{run_id}/{index:06d}"

# The run log's whole vocabulary, declared once. The lifecycle spine:
RUN_START = "run_start"
RUN_DONE = "run_done"
STAGE_START = "stage_start"
STAGE_DONE = "stage_done"
ROW_START = "row_start"
ROW_OK = "row_ok"
ROW_ERROR = "row_error"
# ...and the LLM's own account of a row, emitted at LEVEL_DETAIL:
LLM_PROMPT = "llm_prompt"
LLM_THINKING = "llm_thinking"
LLM_TEXT = "llm_text"
LLM_RESPONSE = "llm_response"
LLM_TOOL_RESULT = "llm_tool_result"
LLM_ERROR = "llm_error"
# The CLI's own account of a turn (its init inventory: connected MCP servers,
# the tools the model was actually offered) — not the model's words, but the
# only record of what the model had to work with.
LLM_SYSTEM = "llm_system"

# Where a row's output came from, stamped on every terminal row event. A cache
# hit ran no code and called no model, so it has no start and no LLM detail —
# `source` is how a reader tells that apart from a row that was actually
# computed, rather than the two looking identical.
SOURCE_CACHED = "cached"
SOURCE_COMPUTED = "computed"

# Verbosity levels. Every event carries one; the run page filters by it.
#   0 = lifecycle: run/stage/row start·done — the coarse "what happened" spine.
#   1 = detail:    the LLM's own prompt/thinking/response for a row.
# emit() defaults an event to LEVEL_LIFECYCLE, so a caller only names a level to
# push something DOWN into the detail tier.
LEVEL_LIFECYCLE = 0
LEVEL_DETAIL = 1


class RunLog:
    """Any thread may emit(); a single writer thread does the writing."""

    def __init__(self, project_id: str, run_id: str):
        self._project = project_id
        self._run_id = run_id
        self._q: queue.Queue[Any] = queue.Queue()
        self._closed = False
        # Set by the writer thread before it reads anything; only it touches this.
        self._open_index = 0
        # Start the writer BEFORE returning so the first emit() is never lost.
        self._writer = threading.Thread(
            target=self._drain, name=f"run-log:{run_id}", daemon=True
        )
        self._writer.start()

    def emit(self, event: dict[str, Any]) -> None:
        # The writer stamps `seq` and `ts`; callers supply `kind` and that kind's fields.
        if self._closed:
            return
        event.setdefault("level", LEVEL_LIFECYCLE)
        self._q.put(event)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Enqueued directly (not via emit(), which early-returns once _closed is
        # set), so stamp the lifecycle level here to keep every line uniform.
        self._q.put({"kind": RUN_DONE, "level": LEVEL_LIFECYCLE})
        self._q.put(_STOP)
        # Bounded join: the writer is daemon, so a stuck flush can never wedge
        # the run thread's teardown.
        self._writer.join(timeout=5.0)

    def _drain(self) -> None:
        """Stamp each event, batch, and flush a chunk at a time."""
        seq, self._open_index = find_log_end(self._project, self._run_id)
        # seq resumes at the run's event count: restarting at 0 would put resumed
        # events behind a tailer's cursor, which drops every one of them. The open
        # index is carried rather than computed from seq, so a run resumed after
        # CHUNK_SIZE changed appends to the chunks it already has instead of
        # landing past them and leaving a hole the dense walk stops at.
        pending: list[JsonDict] = []
        while True:
            item = self._await_next()
            if item is not _STOP and item is not None:
                pending.append({
                    "seq": seq,
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    **item,
                })
                seq += 1
            if pending and (item is None or item is _STOP or len(pending) >= CHUNK_SIZE):
                self._flush(pending)
                pending = []
            if item is _STOP:
                return

    def _await_next(self) -> Any:
        """The next queued item, or None when the flush interval elapses first."""
        try:
            return self._q.get(timeout=_FLUSH_INTERVAL_S)
        except queue.Empty:
            return None

    def _flush(self, pending: list[JsonDict]) -> None:
        """Append `pending` to the open chunk, rolling to the next as each fills."""
        remaining = pending
        while remaining:
            chunk = _load_chunk(self._project, self._run_id, self._open_index)
            if chunk is None:
                chunk = RunEventChunk(
                    id=RunEventChunk.compose_id(
                        self._project, self._run_id, self._open_index),
                    first_seq=int(remaining[0]["seq"]))
            room = CHUNK_SIZE - len(chunk.events)
            if room <= 0:
                # Already at or over the size: a chunk written when CHUNK_SIZE
                # was larger is full by today's measure, so move past it.
                self._open_index += 1
                continue
            chunk.events = [*chunk.events, *remaining[:room]]
            chunk.save()
            remaining = remaining[room:]
            if len(chunk.events) >= CHUNK_SIZE:
                self._open_index += 1


def _load_chunk(project_id: str, run_id: str, index: int) -> RunEventChunk | None:
    return RunEventChunk.load_or_none(RunEventChunk.compose_id(project_id, run_id, index))


def count_events(project_id: str, run_id: str) -> int:
    """How many events this run has already logged."""
    logged, _ = find_log_end(project_id, run_id)
    return logged


def find_log_end(project_id: str, run_id: str) -> tuple[int, int]:
    """How many events this run has logged, and the index of the chunk still open."""
    index, logged = 0, 0
    # Chunk ids are dense from 0, so walking to the first missing one finds the
    # end without listing. The total is READ off the last chunk rather than
    # multiplied out of the index, which is what lets chunks differ in size.
    while True:
        chunk = _load_chunk(project_id, run_id, index)
        if chunk is None:
            return logged, max(0, index - 1)
        logged = chunk.first_seq + len(chunk.events)
        index += 1


def read_events_backward(project_id: str, run_id: str) -> Iterator[list[JsonDict]]:
    """Each stored chunk's events, newest chunk first — for a reader that wants the tail."""
    _, last = find_log_end(project_id, run_id)
    for index in range(last, -1, -1):
        chunk = _load_chunk(project_id, run_id, index)
        if chunk is not None:
            yield chunk.events


def read_events_since(project_id: str, run_id: str, from_seq: int) -> list[dict[str, Any]]:
    """This run's events with seq >= from_seq, in emission order."""
    out: list[dict[str, Any]] = []
    # No chunk has ever held more than _WIDEST_CHUNK_SIZE events, so the chunk
    # at `from_seq // _WIDEST_CHUNK_SIZE` starts at or before from_seq whatever
    # mix of sizes the run was written in: the jump is a floor, never an
    # overshoot. Chunks before it are still never read.
    index = max(0, from_seq // _WIDEST_CHUNK_SIZE)
    while True:
        chunk = _load_chunk(project_id, run_id, index)
        if chunk is None:
            return out
        out.extend(e for e in chunk.events if int(e["seq"]) >= from_seq)
        index += 1


# ── the per-unit detail sink ─────────────────────────────────────────────────
# A row mapper is called as a bare `map_row(row, index)` and a batched chunk as
# `_process_chunk(...)` — neither carries a run log, yet the LLM layer several
# frames below is exactly where the prompt/thinking/response worth logging is
# produced. Rather than thread a log through every mapper signature, the driver
# binds this ContextVar for the duration of one unit of work; deep code pulls the
# bound sink and emits LEVEL_DETAIL events attributed to that (stage, rows).
#
# ContextVar, not a plain global, because rows fan out across worker threads.
# A thread starts with an EMPTY context, so the binding must happen on the
# worker thread itself (it does — inside the mapped callable), and a caller that
# will hop threads again captures the sink synchronously first (see llm.py).


class DetailSink:
    def __init__(self, log: RunLog, stage: str, rows: tuple[int, ...]):
        self._log, self._stage, self._rows = log, stage, rows

    def emit(self, kind: str, **fields: Any) -> None:
        # `row` is the unit's first row, `rows` the whole span — a chunk's prompt is not one row's.
        self._log.emit({
            "kind": kind, "level": LEVEL_DETAIL, "stage": self._stage,
            "row": self._rows[0], "rows": list(self._rows), **fields,
        })


_detail_sink: contextvars.ContextVar[DetailSink | None] = contextvars.ContextVar(
    "run_log_detail_sink", default=None
)

Token = contextvars.Token["DetailSink | None"]


def bind_row_sink(log: RunLog | None, stage: str, row: int) -> Token:
    return bind_detail_sink(log, stage, (row,))


def bind_detail_sink(log: RunLog | None, stage: str, rows: tuple[int, ...]) -> Token:
    if log is None or not rows:
        return _detail_sink.set(None)
    return _detail_sink.set(DetailSink(log, stage, rows))


def unbind_detail_sink(token: Token) -> None:
    _detail_sink.reset(token)


def current_detail_sink() -> DetailSink | None:
    return _detail_sink.get()


def emit_llm_detail(kind: str, **fields: Any) -> None:
    sink = current_detail_sink()
    if sink is not None:
        sink.emit(kind, **fields)
