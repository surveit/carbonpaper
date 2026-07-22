"""Durable, per-run event log — the drill-down companion to manifest.json.

A run executes in a background thread and fans its rows out across a
ThreadPoolExecutor (see app/runtime/stages/execution.py), so lifecycle events
originate on many worker threads at once. Writing them straight to a file from
each worker would mean lock contention on the hot path and seq/line ordering
races. Instead every worker does a lock-free ``emit()`` (a ``queue.put``) and a
single writer thread drains the queue, assigns the monotonic ``seq``, and
appends one JSON object per line to ``runs/<run_id>/events.jsonl``.

That single file is both the live feed (the run page tails it over SSE) and the
historical record (the same file, re-read, is how you investigate a finished
run). The manifest stays the source of truth for stage status; this log is only
ever the drill-down — it never becomes load-bearing.

Scope: single process. The queue is in memory, so a hard crash mid-run can drop
events still queued but not yet written (the manifest still records the stage
outcome). That is the accepted tradeoff for a debug log — not worth a durable
queue.
"""
from __future__ import annotations

import json
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# Sentinel pushed by close() to tell the writer thread to drain and stop.
_STOP = object()

# The event kind that marks the end of a run's stream. The SSE tailer treats a
# line carrying this kind as the definitive "no more events" signal.
RUN_DONE = "run_done"


class RunLog:
    """A thread-safe, file-backed append log for one run.

    Any thread may call ``emit()``; exactly one writer thread touches the file.
    Call ``close()`` once when the run finishes to flush and stop the writer.
    """

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
        """Queue one event. Non-blocking, safe from any worker thread.

        The writer stamps ``seq`` (a monotonic cursor for SSE ``?from=N`` resume)
        and ``ts``; callers supply ``kind`` plus whatever fields that kind needs
        (``stage``, ``row``, ``text``, …)."""
        if self._closed:
            return
        self._q.put(event)

    def close(self) -> None:
        """Emit the terminal ``run_done`` marker, then flush and stop the writer.
        Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._q.put({"kind": RUN_DONE})
        self._q.put(_STOP)
        # Bounded join: the writer is daemon, so a stuck flush can never wedge the
        # run thread's teardown. In practice it drains a handful of buffered lines.
        self._writer.join(timeout=5.0)

    def _drain(self, ) -> None:
        seq = 0
        # Append mode: a resumed run continues its existing log rather than
        # truncating it. line-buffered + flush so the tailer sees events promptly.
        try:
            f = self._path.open("a", encoding="utf-8")
        except OSError:
            return
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    return
                record = {"seq": seq,
                          "ts": datetime.now().isoformat(timespec="milliseconds"),
                          **item}
                seq += 1
                try:
                    f.write(json.dumps(record, default=str) + "\n")
                    f.flush()
                except OSError:
                    # A debug log must never take the run down. Drop and continue;
                    # the manifest remains the authoritative outcome.
                    pass
        finally:
            f.close()


def read_events_since(path: Path, from_seq: int) -> list[dict[str, Any]]:
    """Return the events in ``path`` with ``seq >= from_seq``, in file order.

    Cheap re-read of the JSONL file (the coarse lifecycle log is small); a
    malformed trailing line — possible if we read mid-write — is skipped, and the
    next poll picks it up once complete. Missing file (run just started, writer
    hasn't created it) reads as empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Every event the writer emits carries a seq; a dict without one is
        # skipped rather than assigned a fabricated position.
        seq = ev.get("seq")
        if seq is not None and seq >= from_seq:
            out.append(ev)
    return out
