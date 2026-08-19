"""The app-startup store wiring: the FastAPI lifespan configures a real
SqliteKvStore from CARBON_PAPER_DB_PATH when no store is pre-configured (the autouse
fresh_store fixture pre-empts this in every other test, so cover it here)."""
from __future__ import annotations

import asyncio

import app.core.persistence as persistence
from app.core.sqlite_store import SqliteKvStore
from app.main import app, lifespan
from app.web.startup import maintain_run_reconciliation


def test_lifespan_configures_store_from_env(monkeypatch, tmp_path):
    # Force the lifespan's real branch: clear the store the autouse fixture set.
    monkeypatch.setattr(persistence, "_store", None)
    db_path = tmp_path / "sub" / "app.db"  # nested, to also exercise the parent mkdir
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(db_path))

    async def drive() -> None:
        async with lifespan(app):
            assert persistence.is_store_configured()
            assert db_path.exists()

    asyncio.run(drive())


def test_lifespan_does_not_overwrite_a_configured_store(monkeypatch, tmp_path):
    # If a store is already configured (as in tests), the guard leaves it alone.
    sentinel = SqliteKvStore(":memory:")
    monkeypatch.setattr(persistence, "_store", sentinel)
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(tmp_path / "should_not_be_created.db"))

    async def drive() -> None:
        async with lifespan(app):
            assert persistence.get_store() is sentinel
            assert not (tmp_path / "should_not_be_created.db").exists()

    asyncio.run(drive())


def test_lifespan_reconciles_interrupted_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.web.startup.reconcile_interrupted_runs", lambda: calls.append(True))

    async def drive() -> None:
        async with lifespan(app):
            assert calls == [True]

    asyncio.run(drive())


def test_lifespan_keeps_checking_for_owners_that_expire_later(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.web.startup.reconcile_interrupted_runs", lambda: calls.append(True))
    monkeypatch.setattr("app.web.startup.RECONCILIATION_INTERVAL_SECONDS", 0.001)

    async def drive() -> None:
        async with maintain_run_reconciliation():
            await asyncio.sleep(0.01)
            assert len(calls) > 1

    asyncio.run(drive())
