"""Two executors must never both execute one run. docs/run-leases.md"""
from __future__ import annotations

import threading

import pytest

from app.core.persistence import configure_store, get_store
from app.core.run_lease import RunLease
from app.core.run_status import RunStatus
from app.core.sqlite_store import SqliteKvStore

_RUN = "demo/runs/20260819T145125"


@pytest.fixture
def store(monkeypatch):
    fresh = SqliteKvStore(":memory:")
    configure_store(fresh)
    return fresh


def _expire_every_lease(store: SqliteKvStore) -> None:
    store._conn.execute("UPDATE run_lease SET expires_at = unixepoch() - 1")
    store._conn.commit()


def test_only_one_of_two_executors_claims_a_run(store):
    first = store.take_lease(_RUN, "exec-A", 90)
    second = store.take_lease(_RUN, "exec-B", 90)
    assert first is not None
    assert second is None, "a live lease was handed to a second executor"


def test_a_renewal_holds_the_same_tenure(store):
    claimed = store.take_lease(_RUN, "exec-A", 90)
    renewed = store.renew_lease(claimed, 90)
    assert renewed is not None
    assert renewed.fence == claimed.fence, (
        "a renewal moved the fence, which would refuse the holder's own next write")


def test_a_takeover_needs_the_tenure_to_have_expired(store):
    store.take_lease(_RUN, "exec-A", 90)
    assert store.take_lease(_RUN, "exec-B", 90) is None
    _expire_every_lease(store)
    taken = store.take_lease(_RUN, "exec-B", 90)
    assert taken is not None and taken.fence == 2


def test_the_superseded_executor_cannot_renew_or_write(store):
    stale = store.take_lease(_RUN, "exec-A", 90)
    _expire_every_lease(store)
    store.take_lease(_RUN, "exec-B", 90)

    assert store.renew_lease(stale, 90) is None, "a superseded executor renewed its lease"
    assert store.write_if_held("run", _RUN, {"status": "errors"}, stale) is False, (
        "a superseded executor wrote through the fence")


def test_the_holder_writes_and_the_document_lands(store):
    held = store.take_lease(_RUN, "exec-A", 90)
    assert store.write_if_held("run", _RUN, {"status": "running"}, held) is True
    assert store.read("run", _RUN) == {"status": "running"}


def test_release_is_scoped_to_the_tenure_that_holds_it(store):
    stale = store.take_lease(_RUN, "exec-A", 90)
    _expire_every_lease(store)
    successor = store.take_lease(_RUN, "exec-B", 90)

    store.release_lease(stale)
    assert store.read_lease(_RUN) == successor, (
        "a superseded executor released the lease its successor was using")
    store.release_lease(successor)
    assert store.read_lease(_RUN) is None


def test_racing_threads_produce_exactly_one_winner(store):
    """The claim is one statement, so the race is settled by SQLite and not by timing."""
    winners: list[RunLease] = []
    barrier = threading.Barrier(8)

    def claim() -> None:
        barrier.wait()
        got = get_store().take_lease(_RUN, "exec", 90)
        if got is not None:
            winners.append(got)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"{len(winners)} executors all believed they held the run"


def test_a_manifest_write_inside_a_lost_tenure_is_refused(store, tmp_path):
    """The wiring: `write_manifest` consults the held lease, so no caller threads a token."""
    from app.runtime import lease
    from app.runtime.errors import RunLeaseLost
    from app.runtime.manifest import RunManifest, write_manifest

    manifest = RunManifest(
        id=_RUN, run_id="20260819T145125", started_at="2026-08-19T14:51:25", project="demo",
        workflow_version="v1", human_review_queue_stats={},
        status=RunStatus.RUNNING, stage_records=[])
    with lease.hold(_RUN):
        write_manifest(manifest)                       # ours: lands
        _expire_every_lease(store)
        store.take_lease(_RUN, "exec-B", 90)          # taken over under us
        with pytest.raises(RunLeaseLost):
            write_manifest(manifest)


def test_a_checkpoint_stops_the_executor_once_the_heartbeat_notices(store):
    """The checkpoint reads a flag, so the row loop pays nothing to be interruptible."""
    from app.runtime import lease
    from app.runtime.errors import RunLeaseLost

    with lease.hold(_RUN):
        lease.validate_still_held()                       # held: no objection
        _expire_every_lease(store)
        store.take_lease(_RUN, "exec-B", 90)
        lease.validate_still_held()                       # the heartbeat has not run yet
        held = lease._held.get()
        assert held is not None
        _renew_once(held)
        with pytest.raises(RunLeaseLost):
            lease.validate_still_held()


def _renew_once(held) -> None:
    """One pass of the heartbeat. `_keep_renewing` waits first, so it is not usable here."""
    from app.runtime import lease
    if get_store().renew_lease(held.lease, lease.LEASE_TTL_SECONDS) is None:
        held.lost.set()
