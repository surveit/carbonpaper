from __future__ import annotations

import time
from datetime import timedelta

import app.runtime.run_lease as run_lease
from app.core.persistence import get_store
from app.core.sqlite_store import SqliteKvStore
from app.runtime.run_lease import run_with_execution_lease, try_claim_run_execution


def test_only_one_connection_claims_a_live_lease(tmp_path) -> None:
    path = str(tmp_path / "shared.db")
    first = SqliteKvStore(path)
    second = SqliteKvStore(path)

    assert first.try_claim_lease("lease", "run", "a", "2099-01-01", "2026-01-01")
    assert not second.try_claim_lease(
        "lease", "run", "b", "2099-01-01", "2026-01-01")


def test_takeover_fences_the_expired_owner(tmp_path) -> None:
    path = str(tmp_path / "shared.db")
    first = SqliteKvStore(path)
    second = SqliteKvStore(path)
    assert first.try_claim_lease("lease", "run", "old", "2020-01-01", "2019-01-01")
    first.write("run", "run", {"status": "running"})

    assert second.try_claim_lease(
        "lease", "run", "new", "2099-01-01", "2026-01-01")
    assert not first.renew_lease("lease", "run", "old", "2099-01-01")
    assert not first.release_lease("lease", "run", "old")
    assert not first.write_if_lease_held(
        "run", "run", {"status": "old"}, 1, "lease", "run", "old")
    assert second.write_if_lease_held(
        "run", "run", {"status": "new"}, 1, "lease", "run", "new")
    assert second.read("run", "run") == {"status": "new"}


def test_execution_heartbeat_renews_until_the_worker_finishes(monkeypatch) -> None:
    monkeypatch.setattr(run_lease, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(run_lease, "LEASE_DURATION", timedelta(seconds=1))
    ownership = try_claim_run_execution("project/runs/run")
    assert ownership is not None
    before = get_store().read(run_lease.LEASE_COLLECTION, ownership.run_key)
    observed = []

    def work() -> None:
        time.sleep(0.03)
        observed.append(get_store().read(
            run_lease.LEASE_COLLECTION, ownership.run_key)["expires_at"])

    run_with_execution_lease(ownership, work)

    assert observed[0] > before["expires_at"]
    assert not get_store().exists(run_lease.LEASE_COLLECTION, ownership.run_key)
