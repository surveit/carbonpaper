"""
llm_sdk.py — low-level Claude Code CLI discovery + event-loop plumbing.

Shared by the runtime's llm_transform dispatch (`app.runtime`), the authoring
compiler (`app.compiler`), and the chat engine (`app.core.agent.sdk_engine`,
`app.agent.router`). None of those import each other; each imports this
neutral base, so the CLI-location + sync-drive logic lives in exactly one
place. Pure stdlib — no SDK import, no app imports.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import shutil
from pathlib import Path


def find_cli() -> str | None:
    """Locate the Claude Code CLI. The SDK's own search misses the Windows
    `.local/bin/claude.exe` (it probes `.local/bin/claude` without the
    extension), so we look explicitly and hand the result to `cli_path`."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        home / "AppData" / "Roaming" / "npm" / "claude.cmd",
        home / ".claude" / "local" / "claude",
        home / ".npm-global" / "bin" / "claude",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return str(c)
    return None


# Resolved once at import time; None → the SDK falls back to its own CLI search.
CLI_PATH = find_cli()


def run_sync(coro):
    """Drive a coroutine to completion from sync code. If NO event loop is
    running on this thread (a CLI call, or a ThreadPoolExecutor worker) use
    asyncio.run directly; if one IS running (a FastAPI async route), asyncio.run
    would raise, so run on a fresh worker thread with its own loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()
