"""Fenced ownership for one background production-run attempt."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import uuid4

from app.core.errors import RunExecutionLeaseLost
from app.core.persistence import PersistedModel, get_store

LEASE_COLLECTION = "run_execution_lease"
LEASE_DURATION = timedelta(seconds=30)
HEARTBEAT_INTERVAL_SECONDS = 5.0

_T = TypeVar("_T")


@dataclass(frozen=True)
class RunExecutionOwnership:
    run_key: str
    holder: str


def try_claim_run_execution(
    run_key: str, *, now: datetime | None = None
) -> RunExecutionOwnership | None:
    holder = uuid4().hex
    claimed_at = datetime.now(UTC) if now is None else now
    claimed = get_store().try_claim_lease(
        LEASE_COLLECTION,
        run_key,
        holder,
        _format_time(claimed_at + LEASE_DURATION),
        _format_time(claimed_at),
    )
    return RunExecutionOwnership(run_key, holder) if claimed else None


def require_run_execution(run_key: str) -> RunExecutionOwnership:
    ownership = try_claim_run_execution(run_key)
    if ownership is None:
        raise RunExecutionLeaseLost(f"run '{run_key}' is already executing")
    return ownership


def renew_run_execution(ownership: RunExecutionOwnership) -> None:
    renewed = get_store().renew_lease(
        LEASE_COLLECTION,
        ownership.run_key,
        ownership.holder,
        _format_time(datetime.now(UTC) + LEASE_DURATION),
    )
    if not renewed:
        raise RunExecutionLeaseLost(
            f"run '{ownership.run_key}' no longer belongs to this execution attempt"
        )


def release_run_execution(ownership: RunExecutionOwnership) -> None:
    get_store().release_lease(
        LEASE_COLLECTION, ownership.run_key, ownership.holder)


def run_with_execution_lease(
    ownership: RunExecutionOwnership,
    target: Callable[..., _T],
    *args: object,
) -> _T:
    stop = threading.Event()
    lost: list[RunExecutionLeaseLost] = []
    heartbeat = threading.Thread(
        target=_renew_until_stopped,
        args=(ownership, stop, lost),
        name=f"run-lease:{ownership.run_key}",
        daemon=True,
    )
    heartbeat.start()
    try:
        result = target(*args)
        if lost:
            raise lost[0]
        return result
    finally:
        stop.set()
        heartbeat.join(timeout=HEARTBEAT_INTERVAL_SECONDS + 1)
        release_run_execution(ownership)


def write_with_execution_lease(
    model: PersistedModel, ownership: RunExecutionOwnership
) -> None:
    saved = model.save_if_lease_held(
        LEASE_COLLECTION, ownership.run_key, ownership.holder)
    if not saved:
        raise RunExecutionLeaseLost(
            f"run '{ownership.run_key}' no longer belongs to this execution attempt"
        )


def _renew_until_stopped(
    ownership: RunExecutionOwnership,
    stop: threading.Event,
    lost: list[RunExecutionLeaseLost],
) -> None:
    while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            renew_run_execution(ownership)
        except RunExecutionLeaseLost as exc:
            lost.append(exc)
            return


def _format_time(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")
