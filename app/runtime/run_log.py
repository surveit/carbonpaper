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
from typing import Any, ClassVar

from app.core.errors import PersistenceError
from app.core.persistence import JsonDict, PersistedModel, PersistenceScope

# Sentinel pushed by close() to tell the writer thread to drain and stop.
_STOP = object()

# Events per stored chunk. A 270k-event run is ~540 documents rather than 270k,
# and a reader wanting `seq >= n` goes straight to chunk `n // CHUNK_SIZE`
# because seq is a gapless counter from 0. The open chunk is REWRITTEN on each
# flush, so a write costs one chunk, never the whole log.
CHUNK_SIZE = 500

# How long the writer thread waits for more events before flushing what it has,
# so a slow run's SSE feed does not stall behind an unfilled chunk.
_FLUSH_INTERVAL_S = 0.25


class RunLogFlushError(RuntimeError):
    """A run's event history was not durably written before it finished."""


class RunEventChunk(PersistedModel):
    """One run's events `first_seq .. first_seq + len(events) - 1`."""

    collection: ClassVar[str] = "run_events"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.RUN

    events: list[JsonDict] = []

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
        self._failure: PersistenceError | None = None
        # Start the writer BEFORE returning so the first emit() is never lost.
        self._writer = threading.Thread(
            target=self._write_events, name=f"run-log:{run_id}", daemon=True
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
        self._writer.join(timeout=5.0)
        if self._writer.is_alive():
            raise RunLogFlushError("run event log did not finish writing")
        if self._failure is not None:
            raise RunLogFlushError("run event log could not be written") from self._failure

    def _write_events(self) -> None:
        try:
            self._drain()
        except PersistenceError as exc:
            self._failure = exc

    def _drain(self) -> None:
        """Stamp each event, batch, and flush a chunk at a time."""
        seq = count_events(self._project, self._run_id)
        # seq resumes at the run's event count: restarting at 0 would put resumed
        # events behind a tailer's cursor, which drops every one of them.
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
        """Write `pending` into the chunks it spans, rewriting each whole."""
        for index, events in _group_by_chunk(pending).items():
            # Chunk k holds seqs [k*CHUNK_SIZE, (k+1)*CHUNK_SIZE), so a batch
            # straddling a boundary touches both. Rewriting the open chunk costs
            # one chunk per flush and never the whole log.
            chunk = _load_chunk(self._project, self._run_id, index) or RunEventChunk(
                id=RunEventChunk.compose_id(self._project, self._run_id, index))
            chunk.events = [*chunk.events, *events]
            chunk.save()


def _group_by_chunk(events: list[JsonDict]) -> dict[int, list[JsonDict]]:
    grouped: dict[int, list[JsonDict]] = {}
    for event in events:
        grouped.setdefault(int(event["seq"]) // CHUNK_SIZE, []).append(event)
    return grouped


def _load_chunk(project_id: str, run_id: str, index: int) -> RunEventChunk | None:
    return RunEventChunk.load_or_none(RunEventChunk.compose_id(project_id, run_id, index))


def count_events(project_id: str, run_id: str) -> int:
    """How many events this run has already logged."""
    index = 0
    # Chunk ids are dense from 0, so walking forward to the first partial one
    # finds the end without listing anything.
    while True:
        chunk = _load_chunk(project_id, run_id, index)
        if chunk is None:
            return index * CHUNK_SIZE
        if len(chunk.events) < CHUNK_SIZE:
            return index * CHUNK_SIZE + len(chunk.events)
        index += 1


def read_events_since(project_id: str, run_id: str, from_seq: int) -> list[dict[str, Any]]:
    """This run's events with seq >= from_seq, in emission order."""
    out: list[dict[str, Any]] = []
    # seq is a gapless counter from 0, so the first chunk that can hold
    # `from_seq` is known by arithmetic — no listing, and no reading of the
    # chunks before it.
    index = max(0, from_seq // CHUNK_SIZE)
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
