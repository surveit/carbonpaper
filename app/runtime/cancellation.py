"""Cooperative cancellation for a running workflow run.

A run executes on one process: a web thread handles ``POST …/cancel``
requests, and a daemon run thread executes the workflow
(``app/web/routers/runs.py::run_in_background``). The two threads share
process memory, so cancellation is not a signal delivered down a call stack —
the run thread does not RECEIVE a cancellation. Instead the web thread DROPS a
cancel message into this module's per-run mailbox (request_cancel), and the
run thread CONSUMES it at the runner's checkpoints (between stages, and
mid-fan-out in the row driver — see app/runtime/runner.py and
app/runtime/stages/execution.py).

A message is consumed on read: the first checkpoint to find one pops it and
stops the run, and it is then gone. So cancel is a SIGNAL, not a state — a
cancelled run leaves no lingering "cancelled" flag behind, which is what lets
that same run be resumed (resume re-runs its not-yet-completed stages): the
resume finds an empty mailbox and proceeds. To stop a resumed run, send a
fresh cancel. (The manifest's "cancelled" run status is a separate thing — the
recorded OUTCOME of a run that was stopped, not the live signal.)

A mailbox is keyed by a run's logical identity ``(project, run_id)``, never
its persistence layout (e.g. the run directory path): this module knows
nothing about how or where a run is stored. That keeps cancellation
independent of the persistence model — enforced by the
"app.runtime.cancellation is a stdlib-only leaf" import-linter contract in
pyproject.toml, which forbids this module from importing any other app
module (stdlib only).
"""
from __future__ import annotations

import threading

_pending: set[tuple[str, str]] = set()
_lock = threading.Lock()


class RunCancelled(Exception):
    """Raised on the run thread when it consumes a cancel message for its
    (project, run_id); caught by the runner to stop the run. An internal
    control signal — sibling in spirit to HaltForReview
    (app/runtime/errors.py) — never surfaced to a user as an error."""


def request_cancel(project: str, run_id: str) -> None:
    """Drop a cancel message into (project, run_id)'s mailbox. Called from the
    web thread; idempotent — a run has at most one pending cancel. Takes effect
    the next time the run thread consumes for this key."""
    with _lock:
        _pending.add((project, run_id))


def consume_cancel(project: str, run_id: str) -> bool:
    """Consume (project, run_id)'s cancel message: if one is pending, remove it
    and return True; otherwise return False. Read-once — the message is gone
    after a True, so a later read (e.g. a resume of the same run) does not see
    it. EVERY caller that gets True must stop the run; a consumed message that
    is ignored is a lost cancel."""
    with _lock:
        if (project, run_id) in _pending:
            _pending.discard((project, run_id))
            return True
        return False


def reset() -> None:
    """Empty every mailbox. For test isolation only — production has no reason
    to clear mailboxes wholesale (each is drained by the run it targets; an
    undelivered message is harmless, since run ids are unique per (project,
    second) and so can never be mis-delivered to a later run)."""
    with _lock:
        _pending.clear()
