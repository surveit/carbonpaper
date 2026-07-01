"""
Runtime LLM configuration + backend selection.

Isolated here (rather than inline in `llm.py`) so the env knobs and the
"which backend runs" policy live in one place an org can override without
touching the call machinery.
"""

from __future__ import annotations

import os
import shutil

from . import llm_agent_sdk


class LLMError(Exception):
    """Raised when a live-LLM call fails, or when no backend is available."""


# ── Config knobs (env-overridable) ───────────────────────────────────────────
CLAUDE_BIN = shutil.which("claude") or llm_agent_sdk._CLI_PATH
DEFAULT_MODEL = os.environ.get("CW_LLM_MODEL", "haiku")
DEFAULT_PARALLEL = int(os.environ.get("CW_LLM_PARALLEL", "4"))
DEFAULT_TIMEOUT_S = int(os.environ.get("CW_LLM_TIMEOUT_S", "180"))


def get_llm_call_type() -> str:
    """Pick the LLM backend: ``'agent_sdk'`` | ``'cli'`` | ``'mock'``.

    - ``CW_LLM_FORCE_MOCK=1`` → ``'mock'``.
    - ``CW_LLM_BACKEND`` selects explicitly: ``agent_sdk`` | ``cli`` | ``mock``.
    - default ``auto`` → ``agent_sdk`` if importable, else ``cli``.

    We never silently fall back to the mock. If a real backend is requested (or
    ``auto``) but none is available, we raise — a mock result must never be
    mistaken for a real model answer. ``mock`` is reachable only when the caller
    explicitly asks for it (``CW_LLM_FORCE_MOCK=1`` or ``CW_LLM_BACKEND=mock``).
    """
    if os.environ.get("CW_LLM_FORCE_MOCK") == "1":
        return "mock"
    choice = os.environ.get("CW_LLM_BACKEND", "auto").lower()
    if choice == "mock":
        return "mock"
    if choice == "agent_sdk":
        if llm_agent_sdk.available():
            return "agent_sdk"
        if CLAUDE_BIN:
            return "cli"
        raise LLMError(
            "CW_LLM_BACKEND=agent_sdk but neither the Agent SDK nor the claude "
            "CLI is available."
        )
    if choice == "cli":
        if CLAUDE_BIN:
            return "cli"
        raise LLMError("CW_LLM_BACKEND=cli but the claude CLI is not on PATH.")
    # auto
    if llm_agent_sdk.available():
        return "agent_sdk"
    if CLAUDE_BIN:
        return "cli"
    raise LLMError(
        "No live LLM backend available (the Agent SDK isn't importable and the "
        "claude CLI isn't on PATH). Install claude-agent-sdk / the claude CLI, "
        "or set CW_LLM_FORCE_MOCK=1 to run the offline mock."
    )
