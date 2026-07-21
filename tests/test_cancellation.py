"""Unit tests for the per-run cancel mailbox.

The web thread DROPS a cancel message (request_cancel); the run thread
CONSUMES it (consume_cancel) at its checkpoints — read-once, so a consumed
message is gone (that is what lets a cancelled run be resumed). reset() empties
every mailbox for test isolation (see the autouse fixture in conftest.py). See
app/runtime/cancellation.py for the two-thread, shared-memory design.
"""
from __future__ import annotations

from app.runtime.cancellation import consume_cancel, request_cancel, reset


def test_consume_returns_false_when_no_message_is_pending():
    assert consume_cancel("proj", "run1") is False


def test_request_then_consume_pops_the_message_once():
    request_cancel("proj", "run1")
    assert consume_cancel("proj", "run1") is True   # delivered
    assert consume_cancel("proj", "run1") is False  # read-once: already consumed


def test_mailboxes_are_independent_across_run_ids():
    request_cancel("proj", "run-a")
    assert consume_cancel("proj", "run-b") is False  # a different run: no message
    assert consume_cancel("proj", "run-a") is True   # untouched by the run-b read


def test_same_run_id_different_projects_do_not_collide():
    request_cancel("proj-x", "shared-run-id")
    assert consume_cancel("proj-y", "shared-run-id") is False
    assert consume_cancel("proj-x", "shared-run-id") is True


def test_reset_empties_pending_mailboxes():
    request_cancel("proj", "run-c")
    reset()
    assert consume_cancel("proj", "run-c") is False
