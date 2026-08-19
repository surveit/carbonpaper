"""Runtime LLM configuration + backend availability.
"""

from __future__ import annotations

import importlib.util
import os
import shutil

from app.core.agent.codex_availability import require_codex_backend
from app.core.errors import LLMError
from app.core.llm import DEFAULT_TRANSFORM_MODEL, LLMModel
from app.core.llm_sdk import CLI_PATH


# ── Config knobs (env-overridable) ───────────────────────────────────────────
CLAUDE_BIN = shutil.which("claude") or CLI_PATH
# The model materialized into a new workspace's global LLM-transform setting. It is
# pinned and refused at import when the environment names a model off this menu.
DEFAULT_MODEL = DEFAULT_TRANSFORM_MODEL
DEFAULT_PARALLEL = int(os.environ.get("CARBON_PAPER_LLM_PARALLEL", "4"))

DEFAULT_TIMEOUT_S = int(os.environ.get("CARBON_PAPER_LLM_TIMEOUT_S", "180"))

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
    return CLAUDE_BIN is not None and importlib.util.find_spec("claude_agent_sdk") is not None


def require_agent_backend() -> None:
    if not agent_available():
        raise LLMError(
            "No LLM backend available: claude-agent-sdk isn't importable "
            "or the claude CLI wasn't found. Install both to run "
            "llm_transform stages."
        )


def require_model_backend(model: LLMModel) -> None:
    if model == LLMModel.gpt_5_6_terra:
        require_codex_backend()
        return
    require_agent_backend()
