"""
Runtime LLM configuration + backend availability.

Isolated here (rather than inline in `llm.py`) so the env knobs and the
availability policy live in one place an org can override without touching
the call machinery.
"""

from __future__ import annotations

import importlib.util
import os
import shutil

from app.core.errors import LLMError
from app.core.llm_sdk import CLI_PATH

__all__ = [
    "CLAUDE_BIN", "DEFAULT_MODEL", "DEFAULT_PARALLEL", "DEFAULT_TIMEOUT_S",
    "LLM_BACKEND", "LLMError", "agent_available", "api_available",
    "require_agent_backend", "require_api_backend",
]

# ── Config knobs (env-overridable) ───────────────────────────────────────────
CLAUDE_BIN = shutil.which("claude") or CLI_PATH
DEFAULT_MODEL = os.environ.get("CW_LLM_MODEL", "haiku")
DEFAULT_PARALLEL = int(os.environ.get("CW_LLM_PARALLEL", "4"))
DEFAULT_TIMEOUT_S = int(os.environ.get("CW_LLM_TIMEOUT_S", "180"))
# Which backend runs llm_transform stages: "agent" (Claude Code subscription,
# structured output via a tool loop) or "api" (Messages API, structured output
# via output_config.format — ~200 prompt tokens/call vs the CLI's ~26k harness).
LLM_BACKEND = os.environ.get("CW_LLM_BACKEND", "agent")


def agent_available() -> bool:
    """True when the structured-output agent backend can run: the
    claude-agent-sdk package is importable AND a Claude CLI was located."""
    return CLAUDE_BIN is not None and importlib.util.find_spec("claude_agent_sdk") is not None


def require_agent_backend() -> None:
    """Raise `LLMError` unless the agent backend can run. The agent is the only
    LLM backend — there is no fallback of any kind, so an `llm_transform` stage
    either runs against a real model or fails loudly here."""
    if not agent_available():
        raise LLMError(
            "No LLM backend available: claude-agent-sdk isn't importable "
            "or the claude CLI wasn't found. Install both to run "
            "llm_transform stages."
        )


def api_available() -> bool:
    """True when the Messages API backend can run: the `anthropic` package is
    importable AND an `ANTHROPIC_API_KEY` is set."""
    return (
        bool(os.environ.get("ANTHROPIC_API_KEY"))
        and importlib.util.find_spec("anthropic") is not None
    )


def require_api_backend() -> None:
    """Raise `LLMError` unless the Messages API backend can run — no fallback, so
    an `llm_transform` stage either reaches a real model or fails loudly here."""
    if not api_available():
        raise LLMError(
            "Messages API backend unavailable: set ANTHROPIC_API_KEY and install "
            "the `anthropic` package to run llm_transform stages with CW_LLM_BACKEND=api."
        )
