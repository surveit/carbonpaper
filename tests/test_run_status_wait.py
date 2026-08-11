"""get_run_status waits out a run server-side, because the caller has no clock: without
it, following a run costs one model turn per check and the transcript fills with them.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import run as run_service, workspace
from app.tools import shared
from app.tools.shared import MAX_STATUS_WAIT_SECONDS


@pytest.fixture
def a_project(tmp_path: Path) -> str:
    workspace.set_projects_dir(tmp_path)
    (tmp_path / "demo").mkdir()
    return "demo"


def _clocked(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Sleeps recorded, never taken: the fake clock advances by whatever was asked for."""
    slept: list[float] = []
    now = [0.0]

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    monkeypatch.setattr(shared, "monotonic", lambda: now[0])
    return slept


def _statuses(monkeypatch: pytest.MonkeyPatch, *statuses: str) -> None:
    """Each read returns the next status; the last one repeats forever."""
    remaining = list(statuses)
    monkeypatch.setattr(run_service, "read_run_status", lambda project, run_id: {
        "run_id": run_id, "status": remaining.pop(0) if len(remaining) > 1 else remaining[0],
    })


def test_it_returns_the_moment_the_run_settles(
    a_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept = _clocked(monkeypatch)
    _statuses(monkeypatch, "running", "running", "ok")

    status = asyncio.run(shared.get_run_status(a_project, "r1", wait_seconds=60))

    assert status["status"] == "ok"
    assert len(slept) == 2  # it stopped waiting on the read that answered, not at the cap


def test_no_wait_reads_the_manifest_and_returns_straight_away(
    a_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept = _clocked(monkeypatch)
    _statuses(monkeypatch, "running")

    status = asyncio.run(shared.get_run_status(a_project, "r1"))

    assert status["status"] == "running"
    assert slept == []


def test_a_run_that_never_settles_comes_back_running_at_the_cap(
    a_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`running` back is the answer that the wait ran out — the caller waits again."""
    slept = _clocked(monkeypatch)
    _statuses(monkeypatch, "running")

    status = asyncio.run(shared.get_run_status(a_project, "r1", wait_seconds=10_000))

    assert status["status"] == "running"
    assert sum(slept) == MAX_STATUS_WAIT_SECONDS


def test_an_unknown_project_is_loud_before_any_waiting(
    a_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept = _clocked(monkeypatch)

    with pytest.raises(ValueError):
        asyncio.run(shared.get_run_status("no_such_project", "r1", wait_seconds=60))

    assert slept == []
