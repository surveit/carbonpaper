"""Cooperative cancellation for a running workflow run.

A message is consumed on read, so cancel is a SIGNAL, not a state: a cancelled
run leaves no flag behind and can be resumed (send a fresh cancel to stop it again).
Mailboxes key on a run's logical `(project, run_id)`, never on its persistence layout."""
from __future__ import annotations

import threading

_pending: set[tuple[str, str]] = set()
_lock = threading.Lock()


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
