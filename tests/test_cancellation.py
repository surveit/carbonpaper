"""Unit tests for the process-global cancel-request registry.

The web thread ADDS a (project, run_id) key (request_cancel); the run thread
POLLS it (is_cancelled / raise_if_cancelled) at its checkpoints and forgets it
when the run ends (clear). See app/runtime/cancellation.py for the two-thread,
shared-memory design this registry exists to serve.
"""
from __future__ import annotations

import pytest

from app.runtime.cancellation import (
    RunCancelled,
    clear,
    is_cancelled,
    raise_if_cancelled,
    request_cancel,
)


def test_roundtrip_request_is_cancelled_raise_then_clear():
    assert is_cancelled("proj", "run1") is False
    request_cancel("proj", "run1")
    assert is_cancelled("proj", "run1") is True
    with pytest.raises(RunCancelled):
        raise_if_cancelled("proj", "run1")
    clear("proj", "run1")
    assert is_cancelled("proj", "run1") is False
    raise_if_cancelled("proj", "run1")  # cleared — must not raise


def test_raise_if_cancelled_is_a_noop_when_never_requested():
    assert is_cancelled("proj", "never-cancelled") is False
    raise_if_cancelled("proj", "never-cancelled")  # must not raise


def test_keys_are_independent_across_different_run_ids():
    request_cancel("proj", "run-a")
    try:
        assert is_cancelled("proj", "run-a") is True
        assert is_cancelled("proj", "run-b") is False
    finally:
        clear("proj", "run-a")


def test_same_run_id_different_projects_do_not_collide():
    request_cancel("proj-x", "shared-run-id")
    try:
        assert is_cancelled("proj-x", "shared-run-id") is True
        assert is_cancelled("proj-y", "shared-run-id") is False
    finally:
        clear("proj-x", "shared-run-id")


def test_clear_is_idempotent():
    clear("proj", "never-requested")  # absent key: not an error
    request_cancel("proj", "run-c")
    clear("proj", "run-c")
    clear("proj", "run-c")  # clearing twice: still not an error
    assert is_cancelled("proj", "run-c") is False
