"""
llm_sdk.py — low-level Claude Code CLI discovery + event-loop plumbing.

Shared by the runtime's llm_transform dispatch (`app.runtime`), the generation
bridges (`app.compiler`), and the chat engine (`app.core.agent.sdk_engine`,
`app.web.chat_router`). None of those import each other; each imports this
neutral base, so the CLI-location, nested-session env cleanup, and sync-drive
logic live in exactly one place. Pure stdlib — no SDK import, no app imports.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
from pathlib import Path

# Running the authoring server from INSIDE a Claude Code session leaks that session's
# markers into any `claude` CLI we spawn, and the child CLI then fails to start as a
# "nested" invocation (CLIConnectionError: Failed to start Claude Code). The Agent SDK
# strips CLAUDECODE itself but NOT the session/entrypoint markers (see subprocess_cli:
# env = {**{os.environ - CLAUDECODE}, **options.env} — a merge, so options.env cannot
# UNSET them). Strip them from THIS process's env once, here in the module every CLI
# spawner already imports, so every spawned CLI gets a clean top-level env. Auth/config
# (ANTHROPIC_BASE_URL, CLAUDE_CODE_OAUTH_*, CLAUDE_CONFIG_DIR, credentials) is
# preserved. No-op outside Claude Code.
for _marker in (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_EXECPATH", "AI_AGENT",
):
    os.environ.pop(_marker, None)


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
