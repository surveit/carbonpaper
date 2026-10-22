from __future__ import annotations

import asyncio
import sys

from app.core.errors import EventLoopCannotSpawnSubprocesses

_SUBPROCESS_TRANSPORT = "_make_subprocess_transport"

# uvicorn imports the factory by this dotted path, and `start` passes the same string.
LOOP_FACTORY_PATH = "app.core.event_loop:create_event_loop"


# uvicorn --loop factory: a CLI-engine turn spawns the claude CLI here, which a selector loop cannot.
def create_event_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


def validate_running_loop_can_spawn_subprocesses() -> None:
    loop = asyncio.get_running_loop()
    if can_spawn_subprocesses(loop):
        return
    raise EventLoopCannotSpawnSubprocesses(
        f"this server is running on a {type(loop).__name__}, which cannot start a child "
        "process. A chat turn on the CLI engine spawns the claude CLI on this loop, so "
        "each would fail with an empty 'CLIConnectionError: Failed to start Claude Code:'. "
        f"Start uvicorn with --loop {LOOP_FACTORY_PATH} (./start already passes it). "
        "See docs/event-loop-and-subprocesses.md"
    )


# docs/event-loop-and-subprocesses.md
def can_spawn_subprocesses(loop: asyncio.AbstractEventLoop | type[asyncio.AbstractEventLoop]) -> bool:
    loop_type = loop if isinstance(loop, type) else type(loop)
    return getattr(loop_type, _SUBPROCESS_TRANSPORT, None) is not getattr(
        asyncio.BaseEventLoop, _SUBPROCESS_TRANSPORT
    )
