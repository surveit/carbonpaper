"""Unit tests for the process-global cancel-request registry.

The web thread ADDS a (project, run_id) key (request_cancel); the run thread
POLLS it (is_cancelled) at its checkpoints. Nothing removes a key at runtime —
cancellation is pure signalling; reset() clears the whole registry and is used
only for test isolation (see the autouse fixture in conftest.py). See
app/runtime/cancellation.py for the two-thread, shared-memory design.
"""
from __future__ import annotations

from app.runtime.cancellation import is_cancelled, request_cancel, reset


def test_request_then_is_cancelled():
    assert is_cancelled("proj", "run1") is False
    request_cancel("proj", "run1")
    assert is_cancelled("proj", "run1") is True


def test_keys_are_independent_across_different_run_ids():
    request_cancel("proj", "run-a")
    assert is_cancelled("proj", "run-a") is True
    assert is_cancelled("proj", "run-b") is False


def test_same_run_id_different_projects_do_not_collide():
    request_cancel("proj-x", "shared-run-id")
    assert is_cancelled("proj-x", "shared-run-id") is True
    assert is_cancelled("proj-y", "shared-run-id") is False


def test_reset_clears_the_registry():
    request_cancel("proj", "run-c")
    assert is_cancelled("proj", "run-c") is True
    reset()
    assert is_cancelled("proj", "run-c") is False
