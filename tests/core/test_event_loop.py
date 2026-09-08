from __future__ import annotations

import asyncio

from app.core.event_loop import LOOP_FACTORY_PATH, can_spawn_subprocesses, create_event_loop
from app.core.paths import repo_root


# docs/event-loop-and-subprocesses.md
class LoopInheritingTheBaseStub(asyncio.BaseEventLoop):
    pass


class LoopImplementingTheTransport(asyncio.BaseEventLoop):
    async def _make_subprocess_transport(self, *args: object, **kwargs: object) -> None:
        return None


def test_the_created_loop_can_spawn_subprocesses() -> None:
    loop = create_event_loop()
    try:
        assert can_spawn_subprocesses(loop)
    finally:
        loop.close()


def test_a_loop_inheriting_the_base_stub_cannot_spawn_subprocesses() -> None:
    assert not can_spawn_subprocesses(LoopInheritingTheBaseStub)
    loop = LoopInheritingTheBaseStub()
    try:
        assert not can_spawn_subprocesses(loop)
    finally:
        loop.close()


def test_a_loop_implementing_the_transport_can_spawn_subprocesses() -> None:
    assert can_spawn_subprocesses(LoopImplementingTheTransport)
    loop = LoopImplementingTheTransport()
    try:
        assert can_spawn_subprocesses(loop)
    finally:
        loop.close()


def test_the_launch_script_passes_the_loop_factory_to_uvicorn() -> None:
    start = (repo_root() / "start").read_text(encoding="utf-8")
    assert f"--loop {LOOP_FACTORY_PATH}" in start, (
        "`start` must pass --loop, or uvicorn picks a loop by platform and hands the "
        "--reload supervisor a Windows loop that cannot spawn the claude CLI"
    )
