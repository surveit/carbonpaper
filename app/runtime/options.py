"""Runtime LLM configuration + backend availability.
"""

from __future__ import annotations

import importlib.util
import os
import shutil

from app.core.errors import LLMError
from app.core.llm import LLMModel
from app.core.llm_sdk import CLI_PATH


# ── Config knobs (env-overridable) ───────────────────────────────────────────
CLAUDE_BIN = shutil.which("claude") or CLI_PATH
# What an `llm_transform` naming no model runs on. Pinned like every LLMModel value, and
# refused at import if the override names something off the menu — a stage that omits
# `llm.model` records nothing about which model answered, so the default is the only
# thing left saying what a run's rows were produced by.
DEFAULT_MODEL = LLMModel.parse(
    os.environ.get("CARBONPAPER_LLM_MODEL", LLMModel.claude_haiku_4_5.value),
    source="CARBONPAPER_LLM_MODEL",
)
DEFAULT_PARALLEL = int(os.environ.get("CARBONPAPER_LLM_PARALLEL", "4"))
DEFAULT_TIMEOUT_S = int(os.environ.get("CARBONPAPER_LLM_TIMEOUT_S", "180"))

# A stage granted research tools works on a completely different clock: it searches,
# fetches documents, and reads them before it can answer. The 180s row timeout above
# would kill every such row, so research rows get their own budget.
RESEARCH_TIMEOUT_S = int(os.environ.get("CW_LLM_RESEARCH_TIMEOUT_S", "3600"))
# Every search and fetch costs a turn, so the submit-only cap (max_attempts + 2)
# would end the run mid-investigation.
RESEARCH_MAX_TURNS = int(os.environ.get("CW_LLM_RESEARCH_MAX_TURNS", "80"))
# NOTE: research rows still run at DEFAULT_PARALLEL. Per-stage parallelism would
# need plumbing through LLMTransformHandler, which fixes it at construction; not
# a correctness issue, since cost is per row either way.


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
