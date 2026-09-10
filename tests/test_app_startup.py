"""The app-startup store wiring: the FastAPI lifespan configures a real
SqliteKvStore from CARBON_PAPER_DB_PATH when no store is pre-configured (the autouse
fresh_store fixture pre-empts this in every other test, so cover it here)."""
from __future__ import annotations

import asyncio
from asyncio.selector_events import BaseSelectorEventLoop

import pytest

import app.core.persistence as persistence
from app.core.errors import EventLoopCannotSpawnSubprocesses
from app.core.event_loop import LOOP_FACTORY_PATH, create_event_loop
from app.core.sqlite_store import SqliteKvStore
from app.main import app, lifespan


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


async def _enter_and_leave_lifespan() -> None:
    async with lifespan(app):
        pass


# docs/event-loop-and-subprocesses.md
def test_startup_refuses_a_loop_that_cannot_spawn_subprocesses() -> None:
    loop = BaseSelectorEventLoop()
    try:
        with pytest.raises(EventLoopCannotSpawnSubprocesses, match=LOOP_FACTORY_PATH):
            loop.run_until_complete(_enter_and_leave_lifespan())
    finally:
        loop.close()


def test_startup_completes_on_the_loop_the_factory_builds() -> None:
    loop = create_event_loop()
    try:
        loop.run_until_complete(_enter_and_leave_lifespan())
    finally:
        loop.close()
