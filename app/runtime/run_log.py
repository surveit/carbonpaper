"""Per-run event log: the live-tailed drill-down companion to manifest.json.

Workers emit lock-free; one writer thread appends JSON lines to
runs/<run_id>/events.jsonl, which is both the SSE feed and the historical
record. The manifest stays the source of truth for stage status.
"""
from __future__ import annotations

import contextvars
import json
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# Sentinel pushed by close() to tell the writer thread to drain and stop.
_STOP = object()

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

    def __init__(self, path: Path):
        self._path = path
        self._q: queue.Queue[Any] = queue.Queue()
        self._closed = False
        # Start the writer BEFORE returning so the first emit() is never lost.
        self._writer = threading.Thread(
            target=self._drain, name=f"run-log:{path.parent.name}", daemon=True
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
        try:
            # seq resumes at the line count: restarting at 0 would put resumed
            # events behind a tailer's cursor, which drops every one of them.
            seq = _count_logged_events(self._path)
            handle = self._path.open("a", encoding="utf-8")
        except OSError:
            return
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    return
                record = {
                    "seq": seq,
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    **item,
                }
                seq += 1
                try:
                    handle.write(json.dumps(record, default=str) + "\n")
                    handle.flush()
                except OSError:
                    # A debug log must never take the run down. Drop and
                    # continue; the manifest remains the authoritative outcome.
                    pass
        finally:
            handle.close()


def _count_logged_events(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def read_events_since(path: Path, from_seq: int) -> list[dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                # Every line is parsed rather than `seq` read off the raw text:
                # that would couple this to json.dumps' key order and break silently.
                event = json.loads(line)
            except json.JSONDecodeError:
                # Possible when read mid-write; the next poll picks the line up.
                continue
            # Every event the writer emits carries a seq; a dict without one is
            # skipped rather than assigned a fabricated position.
            seq = event.get("seq")
            if seq is not None and seq >= from_seq:
                out.append(event)
    return out


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
