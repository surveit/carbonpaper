"""
Low-level Claude Code CLI discovery + event-loop plumbing.

Pure stdlib — no SDK import, no app imports.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
from pathlib import Path


def find_cli() -> str | None:
    """The SDK's own search misses Windows `.local/bin/claude.exe` — it probes without the extension."""
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


# Same concern as locating the CLI: making it LAUNCHABLE. When this process itself
# runs inside a Claude Code session, that session's markers leak into any `claude`
# CLI we spawn, and the child then refuses to start as a "nested" invocation
# (CLIConnectionError: Failed to start Claude Code). The Agent SDK strips CLAUDECODE
# itself but NOT the session/entrypoint markers (subprocess_cli merges
# {os.environ - CLAUDECODE} with options.env, and a merge cannot UNSET a variable).
# Strip them from THIS process's env once, here, so every spawned CLI gets a clean
# top-level env. Auth/config (ANTHROPIC_BASE_URL, CLAUDE_CODE_OAUTH_*,
# CLAUDE_CONFIG_DIR, credentials) is preserved. No-op outside Claude Code.
for _marker in (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_EXECPATH", "AI_AGENT",
):
    os.environ.pop(_marker, None)


def run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()
