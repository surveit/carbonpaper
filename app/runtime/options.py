"""Runtime LLM configuration + backend availability.
"""

from __future__ import annotations

import importlib.util
import os
import shutil

from app.core.errors import LLMError
from app.core.llm_sdk import CLI_PATH

__all__ = [
    "CLAUDE_BIN", "DEFAULT_MODEL", "DEFAULT_PARALLEL", "DEFAULT_TIMEOUT_S",
    "LLMError", "agent_available", "require_agent_backend",
]

# ── Config knobs (env-overridable) ───────────────────────────────────────────
CLAUDE_BIN = shutil.which("claude") or CLI_PATH
DEFAULT_MODEL = os.environ.get("CARBONPAPER_LLM_MODEL", "haiku")
DEFAULT_PARALLEL = int(os.environ.get("CARBONPAPER_LLM_PARALLEL", "4"))
DEFAULT_TIMEOUT_S = int(os.environ.get("CARBONPAPER_LLM_TIMEOUT_S", "180"))


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
