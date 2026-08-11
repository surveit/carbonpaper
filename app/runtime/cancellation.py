"""Cooperative cancellation for a running workflow run.

A message is consumed on read, so cancel is a SIGNAL, not a state: a cancelled
run leaves no flag behind and can be resumed (send a fresh cancel to stop it again).
Mailboxes key on a run's logical `(project, run_id)`, never on its persistence layout."""
from __future__ import annotations

import threading

_pending: set[tuple[str, str]] = set()
_lock = threading.Lock()


def request_cancel(project: str, run_id: str) -> None:
    with _lock:
        _pending.add((project, run_id))


def consume_cancel(project: str, run_id: str) -> bool:
    """Read-once: a caller that gets True MUST stop the run, or the cancel is lost."""
    with _lock:
        if (project, run_id) in _pending:
            _pending.discard((project, run_id))
            return True
        return False


def reset() -> None:
    with _lock:
        _pending.clear()
