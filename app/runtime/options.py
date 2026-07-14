"""
Runtime LLM configuration + backend selection.

Isolated here (rather than inline in `llm.py`) so the env knobs and the
"which backend runs" policy live in one place an org can override without
touching the call machinery.
"""

from __future__ import annotations

import importlib.util
import os
import shutil

from app.core.errors import LLMError
from app.core.llm_sdk import CLI_PATH

__all__ = [
    "CLAUDE_BIN", "DEFAULT_MODEL", "DEFAULT_PARALLEL", "DEFAULT_TIMEOUT_S",
    "LLMError", "agent_available", "get_llm_call_type",
]

# ── Config knobs (env-overridable) ───────────────────────────────────────────
CLAUDE_BIN = shutil.which("claude") or CLI_PATH
DEFAULT_MODEL = os.environ.get("CW_LLM_MODEL", "haiku")
DEFAULT_PARALLEL = int(os.environ.get("CW_LLM_PARALLEL", "4"))
DEFAULT_TIMEOUT_S = int(os.environ.get("CW_LLM_TIMEOUT_S", "180"))


def agent_available() -> bool:
    """True when the structured-output agent backend can run: the
    claude-agent-sdk package is importable AND a Claude CLI was located."""
    return CLAUDE_BIN is not None and importlib.util.find_spec("claude_agent_sdk") is not None


def get_llm_call_type() -> str:
    """Pick the LLM backend: ``'agent'`` | ``'mock'``.

    - ``CW_LLM_FORCE_MOCK=1`` → ``'mock'``.
    - ``CW_LLM_BACKEND`` selects explicitly: ``agent`` | ``mock``.
    - default ``auto`` → ``agent`` when available.

    We never silently fall back to the mock. If a live backend is requested (or
    ``auto``) but none is available, we raise — a mock result must never be
    mistaken for a real model answer. ``mock`` is reachable only when the caller
    explicitly asks for it (``CW_LLM_FORCE_MOCK=1`` or ``CW_LLM_BACKEND=mock``).
    """
    if os.environ.get("CW_LLM_FORCE_MOCK") == "1":
        return "mock"
    choice = os.environ.get("CW_LLM_BACKEND", "auto").lower()
    if choice == "mock":
        return "mock"
    if choice in ("auto", "agent"):
        if agent_available():
            return "agent"
        raise LLMError(
            "No live LLM backend available (claude-agent-sdk isn't importable "
            "or the claude CLI wasn't found). Install them, or set "
            "CW_LLM_FORCE_MOCK=1 to run the offline mock."
        )
    raise LLMError(f"CW_LLM_BACKEND={choice!r}: expected one of agent, mock, auto")
