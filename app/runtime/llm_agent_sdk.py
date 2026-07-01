"""
Agent SDK backend for llm_transform stages — uses `claude_agent_sdk.query()`
instead of shelling out to `claude -p` ourselves.

One `query()` per row, with no inherited CLAUDE.md/settings (`setting_sources=[]`)
and no system prompt of our own. By default the model answers from its own
knowledge with no tools; a stage may pass `tools` (e.g. WebSearch/WebFetch) to
allow just those. The SDK drives the Claude Code CLI subprocess under the hood;
we locate the CLI (it isn't always on PATH on Windows) and pass it via
`cli_path`. Returns the model's raw text; the caller parses JSON.

Selected by app.runtime.llm when claude_agent_sdk is importable. Set
CW_LLM_BACKEND=cli to force the subprocess path, or CW_LLM_FORCE_MOCK=1 for the
offline mock.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
from pathlib import Path
from typing import Any

_TIMEOUT_S = int(os.environ.get("CW_LLM_TIMEOUT_S", "180"))

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )
    _AVAILABLE = True
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - import guard
    _AVAILABLE = False
    _IMPORT_ERROR = repr(exc)


# Tools that are safe + useful for the research path. Anything not listed is
# blocked (default permission mode + an allowlist), so the agent can't touch the
# filesystem or run shell commands.
RESEARCH_TOOLS = ["WebSearch", "WebFetch"]


def _find_cli() -> str | None:
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


_CLI_PATH = _find_cli()


def available() -> bool:
    """True if the SDK is importable AND a CLI was located."""
    return _AVAILABLE and _CLI_PATH is not None


def status() -> dict:
    return {
        "sdk_importable": _AVAILABLE,
        "import_error": _IMPORT_ERROR,
        "cli_path": _CLI_PATH,
    }


def _trunc(v, n: int = 600) -> str:
    s = v if isinstance(v, str) else str(v)
    return s if len(s) <= n else s[:n] + f"... (+{len(s) - n} chars)"


async def _aquery(
    prompt: str,
    model: str,
    *,
    tools=None,
    system: str | None = None,
    on_event=None,
    max_turns: int | None = None,
):
    """Run one query. Returns (final_text, events).

    `tools`: tool names to ALLOW (e.g. RESEARCH_TOOLS). Empty/None → no tools,
    the model answers from training knowledge in one turn (default permission
    mode + empty allowlist blocks all tools; bypassPermissions would instead
    auto-approve WebSearch/WebFetch and trigger a research loop).
    `on_event(ev)`: called live as thinking / tool_use / tool_result / text
    events stream — lets the inspector show the agent working instead of running
    silently. `events`: the same list, accumulated and returned."""
    allow = list(tools or [])
    use_tools = bool(allow)

    opts_kwargs: dict[str, Any] = dict(
        model=model,
        max_turns=max_turns or (16 if use_tools else 4),
        allowed_tools=allow,
        setting_sources=[],
    )
    # No default system prompt; applied only when the caller passes `system`.
    if system:
        opts_kwargs["system_prompt"] = system
    if _CLI_PATH:
        opts_kwargs["cli_path"] = _CLI_PATH
    options = ClaudeAgentOptions(**opts_kwargs)

    text = ""
    events: list[dict] = []

    def emit(ev: dict) -> None:
        events.append(ev)
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text += block.text
                        emit({"kind": "text", "text": block.text})
                    elif isinstance(block, ThinkingBlock):
                        emit({"kind": "thinking", "text": getattr(block, "thinking", "")})
                    elif isinstance(block, ToolUseBlock):
                        emit({"kind": "tool_use", "name": block.name,
                              "input": _trunc(block.input, 400)})
            elif isinstance(msg, UserMessage):
                for block in getattr(msg, "content", []) or []:
                    if isinstance(block, ToolResultBlock):
                        emit({"kind": "tool_result", "content": _trunc(block.content, 500)})
            elif isinstance(msg, ResultMessage):
                emit({"kind": "result",
                      "is_error": getattr(msg, "is_error", False),
                      "num_turns": getattr(msg, "num_turns", None)})
    except Exception as exc:
        emit({"kind": "error", "text": str(exc)})
        if not text.strip():
            raise
    return text, events


def _run_sync(coro):
    """Run an async coroutine to completion from sync code. Safe inside the
    ThreadPoolExecutor workers used by call_llm_batch (no running loop there);
    if a loop *is* running on this thread, run on a fresh worker thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def call_agent_sdk(prompt: str, model: str) -> str:
    """Synchronous entry point for the runner: tool-less completion → raw text."""
    if not _AVAILABLE:
        raise RuntimeError(f"claude_agent_sdk not importable: {_IMPORT_ERROR}")
    if _CLI_PATH is None:
        raise RuntimeError("Claude Code CLI not found for agent SDK backend")
    text, _ = _run_sync(asyncio.wait_for(_aquery(prompt, model), timeout=_TIMEOUT_S))
    return text


def run_query(
    prompt: str,
    model: str = "haiku",
    *,
    tools=None,
    system: str | None = None,
    on_event=None,
    timeout: int | None = None,
) -> dict:
    """Rich entry point for the inspector REPL: returns {text, events}. Pass
    tools=RESEARCH_TOOLS to let the agent use the web to find real sources."""
    if not available():
        raise RuntimeError("agent SDK backend unavailable")
    text, events = _run_sync(
        asyncio.wait_for(
            _aquery(prompt, model, tools=tools, system=system, on_event=on_event),
            timeout=timeout or (600 if tools else _TIMEOUT_S),
        )
    )
    return {"text": text, "events": events}
