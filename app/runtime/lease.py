"""docs/run-leases.md"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator
from uuid import uuid4

from app.core.ids import ID
from app.core.persistence import get_store
from app.core.run_lease import RunLease
from app.runtime.errors import RunLeaseLost

logger = logging.getLogger(__name__)

# Long on purpose — docs/run-leases.md
LEASE_TTL_SECONDS = 90
_RENEW_EVERY_SECONDS = 20

# Names who holds a lease when one is read back; the fence is what makes takeover safe.
EXECUTOR_ID = f"{os.getpid()}-{uuid4().hex[:8]}"


@dataclass(frozen=True)
class _Held:
    lease: RunLease
    # Set by the heartbeat when a renewal is refused, so a checkpoint costs no query.
    lost: threading.Event


_held: ContextVar[_Held | None] = ContextVar("held_run_lease", default=None)
_live: set[RunLease] = set()
_live_lock = threading.Lock()


@contextmanager
def hold(run_id: ID) -> Iterator[RunLease]:
    """Claim, heartbeat until the body exits, release. Raises RunLeaseLost if someone holds it."""
    lease = get_store().claim_lease(run_id, EXECUTOR_ID, LEASE_TTL_SECONDS)
    if lease is None:
        raise RunLeaseLost(f"run {run_id} is already being executed by another process")
    held = _Held(lease=lease, lost=threading.Event())
    stop = threading.Event()
    token = _held.set(held)
    with _live_lock:
        _live.add(lease)
    threading.Thread(target=_keep_renewing, args=(held, stop), daemon=True).start()
    try:
        yield lease
    finally:
        stop.set()
        _held.reset(token)
        with _live_lock:
            _live.discard(lease)
        get_store().release_lease(lease)


def current() -> RunLease | None:
    """None outside a production run — an eval run and a stage test hold no lease."""
    held = _held.get()
    return held.lease if held is not None else None


def validate_still_held() -> None:
    """Checkpoint. Reads an in-memory flag, so the row loop may call it per group."""
    held = _held.get()
    if held is not None and held.lost.is_set():
        raise RunLeaseLost(f"run {held.lease.run_id} was taken over by another executor")


def _keep_renewing(held: _Held, stop: threading.Event) -> None:
    """Its own thread, so waiting out a rate limit is never mistaken for death."""
    while not stop.wait(_RENEW_EVERY_SECONDS):
        if get_store().renew_lease(held.lease, LEASE_TTL_SECONDS) is None:
            logger.warning("lost the execution lease on run %s", held.lease.run_id)
            held.lost.set()
            return


def end_tenures_on_shutdown() -> None:
    """A replaced process ends its tenures rather than leaving a successor to wait out the TTL."""
    with _live_lock:
        ending = list(_live)
    for lease in ending:
        logger.info("ending tenure on run %s for shutdown", lease.run_id)
        get_store().expire_lease(lease)
