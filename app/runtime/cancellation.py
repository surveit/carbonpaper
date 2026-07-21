"""Cooperative cancellation for a running workflow run.

A run executes on one process: a web thread handles ``POST …/cancel``
requests, and a daemon run thread executes the workflow
(``app/web/routers/runs.py::run_in_background``). The two threads share
process memory, so cancellation is not a signal delivered down a call stack —
the run thread does not RECEIVE a cancellation. Instead it POLLS this
module's shared registry, keyed by its own logical identity ``(project,
run_id)``, at the runner's checkpoints (between stages, and mid-fan-out in the
row driver — see app/runtime/runner.py and app/runtime/stages/execution.py).
The web thread only ever ADDS a key to the registry; nothing removes one at
runtime. Cancellation is pure signalling — a stopped run's key is inert (run
ids are unique per (project, second), so it can never match a later run), so
the registry needs no lifecycle management from the runner. reset() exists
only to isolate tests.

The key is a run's logical identity, never its persistence layout (e.g. the
run directory path): this module knows nothing about how or where a run is
stored, only that a run is identified by ``(project, run_id)``. That keeps
the cancel registry independent of the persistence model — enforced by the
"app.runtime.cancellation is a stdlib-only leaf" import-linter contract in
pyproject.toml, which forbids this module from importing any other app
module (stdlib only).
"""
from __future__ import annotations

import threading

_cancelled: set[tuple[str, str]] = set()
_lock = threading.Lock()


class RunCancelled(Exception):
    """Raised on the run thread when a cancel has been requested for its
    (project, run_id); caught by the runner to stop the run. An internal
    control signal — sibling in spirit to HaltForReview
    (app/runtime/stages/_shared.py) — never surfaced to a user as an error."""


def request_cancel(project: str, run_id: str) -> None:
    """Record a cancel request for (project, run_id). Called from the web
    thread; takes effect the next time the run thread polls is_cancelled for
    this same key."""
    with _lock:
        _cancelled.add((project, run_id))


def is_cancelled(project: str, run_id: str) -> bool:
    """True if a cancel has been requested for (project, run_id). Membership
    reads are GIL-atomic, so this does not need the lock (only add/discard
    do)."""
    return (project, run_id) in _cancelled


def reset() -> None:
    """Clear the entire registry. For test isolation only — production code
    never removes keys. A cancelled run leaves its (project, run_id) key in
    place, which is harmless: run ids are unique per (project, second), so a
    stale key can never match a later run."""
    with _lock:
        _cancelled.clear()
